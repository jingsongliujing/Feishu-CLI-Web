import json
import time
import uuid
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.api.routes.auth import AccountInfo, get_current_account
from app.core.execution_records import execution_record_store
from app.core.local_sessions import session_store
from app.core.scheduled_tasks import parse_schedule_intent, scheduled_task_config_store, scheduled_task_store
from app.skills.base import SkillContext, SkillResult
from app.skills.lark_cli.skill import LarkCLISkill

router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    user_id: str = "local"
    session_id: str = ""
    command: str = ""
    confirm_write: bool = False
    confirm_plan: bool = False
    timeout: int | None = None
    stream: bool = True


class PlanPreviewRequest(BaseModel):
    message: str = Field(..., min_length=1)
    user_id: str = "local"
    session_id: str = ""


def _serialize_sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _result_payload(result: SkillResult) -> dict[str, Any]:
    return {
        "type": "final",
        "success": result.success,
        "content": result.message,
        "metadata": result.data or {},
        "need_continue": result.need_continue,
    }


def _scheduled_task_message(task: dict[str, Any]) -> str:
    next_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(task["next_run_at"])))
    label = "每天" if task.get("schedule_type") == "daily" else "一次性"
    return (
        f"已创建{label}定时任务。\n\n"
        f"- 任务 ID：{task['id']}\n"
        f"- 执行内容：{task['task_message']}\n"
        f"- 下次执行：{next_text}（{task.get('timezone') or 'Asia/Shanghai'}）\n\n"
        "到达时间后，系统会自动执行该飞书任务，并把执行结果写入当前会话和执行记录。"
    )


def _schedule_confirmation_message(intent: Any) -> str:
    preview = intent.to_preview()
    return (
        "检测到这是一个定时任务，请先确认后再创建：\n\n"
        f"- 执行内容：{preview['task_message']}\n"
        f"- 触发类型：{'每天重复' if preview['schedule_type'] == 'daily' else '一次性'}\n"
        f"- 下次执行：{preview['next_run_at_text']}（{preview['timezone']}）\n\n"
        "如果确认创建，请通过执行计划预览点击“确认执行”。"
    )


def _schedule_disabled_message() -> str:
    return "定时任务当前已关闭。请先在输入框上方的「定时任务」面板打开开关，再创建新的定时任务。"


@router.post("/chat")
async def chat(request: ChatRequest, account: AccountInfo = Depends(get_current_account)):
    settings = get_settings()
    skill = LarkCLISkill()
    resolved_user_id = account.account
    session = session_store.get_or_create(resolved_user_id, request.session_id)
    session_id = str(session["session_id"])
    context = SkillContext(
        session_id=session_id,
        user_id=resolved_user_id,
        message=request.message,
        history=session_store.history_for_context(session),
        metadata={"created_at": int(time.time()), "account_name": account.name},
    )
    timeout = request.timeout or settings.LARK_CLI_COMMAND_TIMEOUT
    schedule_intent = parse_schedule_intent(request.message)
    schedule_enabled = scheduled_task_config_store.enabled()

    if not request.stream:
        if schedule_intent and not schedule_enabled:
            message = _schedule_disabled_message()
            metadata = {"schedule_disabled": True, "schedule": schedule_intent.to_preview()}
            session_store.append_message(context.user_id, context.session_id, "user", request.message)
            session_store.append_message(context.user_id, context.session_id, "assistant", message, metadata)
            return {"code": 0, "data": {"type": "final", "success": False, "content": message, "metadata": metadata}}

        if schedule_intent and not request.confirm_plan:
            message = _schedule_confirmation_message(schedule_intent)
            metadata = {"schedule_required": True, "schedule": schedule_intent.to_preview()}
            session_store.append_message(context.user_id, context.session_id, "user", request.message)
            session_store.append_message(context.user_id, context.session_id, "assistant", message, metadata)
            return {"code": 0, "data": {"type": "final", "success": False, "content": message, "metadata": metadata}}

        if schedule_intent and request.confirm_plan:
            task = scheduled_task_store.add(user_id=context.user_id, session_id=context.session_id, intent=schedule_intent)
            message = _scheduled_task_message(task)
            metadata = {"scheduled_task_created": True, "scheduled_task": task}
            session_store.append_message(context.user_id, context.session_id, "user", request.message)
            session_store.append_message(context.user_id, context.session_id, "assistant", message, metadata)
            execution_record_store.add(
                user_id=context.user_id,
                session_id=context.session_id,
                request=request.message,
                plan={"type": "scheduled_task", "schedule": schedule_intent.to_preview()},
                executed_commands=[],
                success=True,
            )
            return {"code": 0, "data": {"type": "final", "success": True, "content": message, "metadata": metadata}}

        result = await skill.execute(
            context,
            query=request.message,
            command=request.command or None,
            confirm_write=request.confirm_write,
            timeout=timeout,
        )
        session_store.append_message(context.user_id, context.session_id, "user", request.message)
        session_store.append_message(
            context.user_id,
            context.session_id,
            "assistant",
            result.message,
            result.data or {},
        )
        execution_record_store.add(
            user_id=context.user_id,
            session_id=context.session_id,
            request=request.message,
            plan=(result.data or {}).get("plan") or {},
            executed_commands=(result.data or {}).get("executed_commands") or [],
            success=result.success,
        )
        return {"code": 0, "data": _result_payload(result)}

    async def event_stream() -> AsyncGenerator[str, None]:
        full_response = ""
        final_metadata: dict[str, Any] = {}
        progress_events: list[str] = []
        try:
            yield _serialize_sse({"type": "session", "session_id": context.session_id})
            if schedule_intent and not schedule_enabled:
                full_response = _schedule_disabled_message()
                final_metadata = {"schedule_disabled": True, "schedule": schedule_intent.to_preview()}
                session_store.append_message(context.user_id, context.session_id, "user", request.message)
                session_store.append_message(context.user_id, context.session_id, "assistant", full_response, final_metadata)
                yield _serialize_sse({"type": "metadata", "metadata": final_metadata})
                yield _serialize_sse({"type": "content", "content": full_response})
                yield _serialize_sse({"type": "done", "session_id": context.session_id})
                return
            if schedule_intent and request.confirm_plan:
                task = scheduled_task_store.add(user_id=context.user_id, session_id=context.session_id, intent=schedule_intent)
                full_response = _scheduled_task_message(task)
                final_metadata = {"scheduled_task_created": True, "scheduled_task": task}
                session_store.append_message(context.user_id, context.session_id, "user", request.message)
                session_store.append_message(context.user_id, context.session_id, "assistant", full_response, final_metadata)
                execution_record_store.add(
                    user_id=context.user_id,
                    session_id=context.session_id,
                    request=request.message,
                    plan={"type": "scheduled_task", "schedule": schedule_intent.to_preview()},
                    executed_commands=[],
                    success=True,
                )
                yield _serialize_sse({"type": "metadata", "metadata": final_metadata})
                yield _serialize_sse({"type": "content", "content": full_response})
                yield _serialize_sse({"type": "done", "session_id": context.session_id})
                return
            if schedule_intent and not request.confirm_plan:
                full_response = _schedule_confirmation_message(schedule_intent)
                final_metadata = {"schedule_required": True, "schedule": schedule_intent.to_preview()}
                session_store.append_message(context.user_id, context.session_id, "user", request.message)
                session_store.append_message(context.user_id, context.session_id, "assistant", full_response, final_metadata)
                yield _serialize_sse({"type": "metadata", "metadata": final_metadata})
                yield _serialize_sse({"type": "content", "content": full_response})
                yield _serialize_sse({"type": "done", "session_id": context.session_id})
                return
            async for event in skill.execute_stream(
                context,
                query=request.message,
                command=request.command or None,
                confirm_write=request.confirm_write,
                timeout=timeout,
            ):
                if event.get("type") == "content":
                    content = event.get("content", "")
                    full_response += str(content or "")
                    yield _serialize_sse({"type": "content", "content": content})
                elif event.get("type") == "metadata":
                    final_metadata.update(event.get("data") or {})
                    yield _serialize_sse({"type": "metadata", "metadata": event.get("data") or {}})
                else:
                    content = str(event.get("content") or "")
                    if content:
                        progress_events.append(content)
                    yield _serialize_sse({"type": "progress", "content": content})
            if progress_events:
                final_metadata["lark_progress"] = progress_events
                final_metadata["execution_trace"] = progress_events
            session_store.append_message(context.user_id, context.session_id, "user", request.message)
            session_store.append_message(context.user_id, context.session_id, "assistant", full_response, final_metadata)
            execution_record_store.add(
                user_id=context.user_id,
                session_id=context.session_id,
                request=request.message,
                plan=final_metadata.get("plan") or {},
                executed_commands=final_metadata.get("executed_commands") or [],
                success=bool(final_metadata.get("executed_commands")) and not final_metadata.get("setup_required"),
            )
            yield _serialize_sse({"type": "done", "session_id": context.session_id})
        except Exception as exc:
            yield _serialize_sse(
                {
                    "type": "error",
                    "content": f"Feishu CLI request failed: {exc}",
                }
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/plan")
async def preview_chat_plan(
    request: PlanPreviewRequest,
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    resolved_user_id = account.account
    session = session_store.get_session(resolved_user_id, request.session_id) if request.session_id else None
    context = SkillContext(
        session_id=str(session["session_id"]) if session else "",
        user_id=resolved_user_id,
        message=request.message,
        history=session_store.history_for_context(session) if session else [],
        metadata={"created_at": int(time.time()), "account_name": account.name},
    )
    skill = LarkCLISkill()
    plan = await skill.preview_plan(context, request.message)
    schedule_intent = parse_schedule_intent(request.message)
    if schedule_intent:
        schedule = schedule_intent.to_preview()
        schedule_enabled = scheduled_task_config_store.enabled()
        if not schedule_enabled:
            plan.update(
                {
                    "summary": "定时任务当前已关闭，无法创建新的后台任务。",
                    "intent_type": "scheduled_task",
                    "need_confirmation": False,
                    "reason_for_confirmation": "请先在「定时任务」面板打开全局开关。",
                    "schedule": schedule,
                    "commands": [],
                    "schedule_disabled": True,
                }
            )
            return {"code": 0, "data": {"session_id": context.session_id, "plan": plan}}
        plan.update(
            {
                "summary": f"创建定时任务：{schedule['task_message']}，下次执行时间 {schedule['next_run_at_text']}",
                "intent_type": "scheduled_task",
                "need_confirmation": True,
                "reason_for_confirmation": "该请求会创建一个后台定时任务，到点后自动执行飞书操作，需要先确认。",
                "schedule": schedule,
                "commands": [
                    {
                        "command": "scheduled-task:create",
                        "reason": "将用户请求保存为定时任务，由后台调度器按计划执行。",
                        "expected": "scheduled_task",
                        "write": True,
                    }
                ],
            }
        )
    return {"code": 0, "data": {"session_id": context.session_id, "plan": plan}}


@router.get("/sessions")
async def list_sessions(
    user_id: str = Query("local"),
    limit: int = Query(50, ge=1, le=200),
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    return {"code": 0, "data": session_store.list_sessions(account.account, limit)}


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    user_id: str = Query("local"),
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    session = session_store.get_session(account.account, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"code": 0, "data": session}


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    user_id: str = Query("local"),
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    session = session_store.get_session(account.account, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"code": 0, "data": {"messages": session.get("messages", [])}}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user_id: str = Query("local"),
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    if not session_store.delete_session(account.account, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"code": 0, "message": "Session deleted"}
