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
from app.skills.base import SkillContext, SkillResult
from app.skills.lark_cli.skill import LarkCLISkill

router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    user_id: str = "local"
    session_id: str = ""
    command: str = ""
    confirm_write: bool = False
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

    if not request.stream:
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
