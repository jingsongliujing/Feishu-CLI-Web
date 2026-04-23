from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.core.execution_records import execution_record_store
from app.core.local_sessions import LocalSessionStore, session_store
from app.core.storage import store
from app.skills.base import SkillContext
from app.skills.lark_cli.skill import LarkCLISkill

DEFAULT_TIMEZONE = "Asia/Shanghai"
SCHEDULED_TASK_CONFIG_KEY = "scheduled_tasks"
SCHEDULE_KEYWORDS = ("定时", "每天", "每日", "明天", "后天", "提醒我", "到点", "定期")
TIME_RE = re.compile(
    r"(?:(上午|早上|中午|下午|晚上|凌晨)\s*)?"
    r"([0-2]?\d|[一二两三四五六七八九十]{1,3})"
    r"\s*([:：点时])\s*"
    r"([0-5]?\d|半|[一二两三四五六七八九十]{1,3})?"
)
DATE_RE = re.compile(r"(20\d{2})\s*年\s*([01]?\d)\s*月\s*([0-3]?\d)\s*[日号]?")


@dataclass
class ScheduleIntent:
    original_request: str
    task_message: str
    schedule_type: str
    next_run_at: int
    time_of_day: str
    timezone: str = DEFAULT_TIMEZONE
    max_runs: int | None = None

    def to_preview(self) -> dict[str, Any]:
        run_at = datetime.fromtimestamp(self.next_run_at, ZoneInfo(self.timezone))
        return {
            "original_request": self.original_request,
            "task_message": self.task_message,
            "schedule_type": self.schedule_type,
            "time_of_day": self.time_of_day,
            "timezone": self.timezone,
            "next_run_at": self.next_run_at,
            "next_run_at_text": run_at.strftime("%Y-%m-%d %H:%M:%S"),
            "max_runs": self.max_runs,
        }


def _cn_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if value.startswith("十"):
        tail = value[1:]
        return 10 + digits.get(tail, 0)
    if "十" in value:
        head, tail = value.split("十", 1)
        return digits.get(head, 0) * 10 + digits.get(tail, 0)
    return digits.get(value)


def _parse_time(text: str) -> tuple[int, int, str] | None:
    match = TIME_RE.search(text)
    if not match:
        return None
    period, hour_raw, _separator, minute_raw = match.groups()
    hour = _cn_number(hour_raw)
    if hour is None or hour > 24:
        return None
    if minute_raw in {None, ""}:
        minute = 0
    elif minute_raw == "半":
        minute = 30
    else:
        minute = _cn_number(minute_raw)
        if minute is None:
            return None
    if minute > 59:
        return None

    if period in {"下午", "晚上"} and 1 <= hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0
    elif hour == 24 and minute == 0:
        hour = 0
    elif hour >= 24:
        return None
    return hour, minute, f"{hour:02d}:{minute:02d}"


def _strip_schedule_text(text: str) -> str:
    cleaned = text.strip()
    patterns = [
        r"(从)?每天\s*(上午|早上|中午|下午|晚上|凌晨)?\s*[0-2]?\d\s*[:：点时]?\s*[0-5]?\d?\s*(开始|的时候)?",
        r"(从)?每日\s*(上午|早上|中午|下午|晚上|凌晨)?\s*[0-2]?\d\s*[:：点时]?\s*[0-5]?\d?\s*(开始|的时候)?",
        r"在?\s*(明天|后天|今天)\s*(上午|早上|中午|下午|晚上|凌晨)?\s*[0-2]?\d\s*[:：点时]?\s*[0-5]?\d?\s*(的时候)?",
        r"在?\s*20\d{2}\s*年\s*[01]?\d\s*月\s*[0-3]?\d\s*[日号]?\s*(上午|早上|中午|下午|晚上|凌晨)?\s*[0-2]?\d\s*[:：点时]?\s*[0-5]?\d?\s*(的时候)?",
        r"定时(任务)?",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned)
    cleaned = re.sub(r"^(请)?帮我?", "", cleaned)
    cleaned = re.sub(r"^[，,。；;\s]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or text.strip()


def parse_schedule_intent(message: str, now: datetime | None = None, timezone: str = DEFAULT_TIMEZONE) -> ScheduleIntent | None:
    text = (message or "").strip()
    if not text:
        return None
    if not any(keyword in text for keyword in SCHEDULE_KEYWORDS):
        return None

    tz = ZoneInfo(timezone)
    current = now.astimezone(tz) if now else datetime.now(tz)
    date_match = DATE_RE.search(text)
    time_text = text
    for marker in ("每天", "每日", "今天", "明天", "后天"):
        if marker in text:
            time_text = text.split(marker, 1)[1]
            break
    if date_match:
        time_text = text[date_match.end() :]
    parsed_time = _parse_time(time_text)
    if not parsed_time:
        return None
    hour, minute, time_of_day = parsed_time

    schedule_type = ""
    max_runs: int | None = None
    if "每天" in text or "每日" in text:
        schedule_type = "daily"
        run_at = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_at <= current:
            run_at += timedelta(days=1)
    else:
        schedule_type = "once"
        max_runs = 1
        if date_match:
            year, month, day = (int(item) for item in date_match.groups())
            run_at = datetime(year, month, day, hour, minute, tzinfo=tz)
            if run_at <= current:
                return None
        elif "后天" in text:
            target = current + timedelta(days=2)
            run_at = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
        elif "明天" in text:
            target = current + timedelta(days=1)
            run_at = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
        elif "今天" in text or "定时" in text:
            run_at = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_at <= current:
                return None
        else:
            return None

    task_message = _strip_schedule_text(text)
    if task_message == text and schedule_type == "daily":
        task_message = re.sub(r"(每天|每日)", "", task_message, count=1).strip(" ，,。；;") or text

    return ScheduleIntent(
        original_request=text,
        task_message=task_message,
        schedule_type=schedule_type,
        next_run_at=int(run_at.timestamp()),
        time_of_day=time_of_day,
        timezone=timezone,
        max_runs=max_runs,
    )


class ScheduledTaskStore:
    def add(self, *, user_id: str, session_id: str, intent: ScheduleIntent) -> dict[str, Any]:
        safe_user = LocalSessionStore._safe_user_id(user_id)
        now = store.now()
        task_id = store.execute_insert(
            """
            INSERT INTO scheduled_tasks(
                user_id, session_id, original_request, task_message, schedule_type,
                time_of_day, timezone, next_run_at, status, max_runs, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                safe_user,
                session_id,
                intent.original_request,
                intent.task_message,
                intent.schedule_type,
                intent.time_of_day,
                intent.timezone,
                intent.next_run_at,
                intent.max_runs,
                now,
                now,
            ),
        )
        row = store.query_one("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        return self._row_to_dict(row) if row else {}

    def due_tasks(self, now_ts: int | None = None, limit: int = 10) -> list[dict[str, Any]]:
        rows = store.query_all(
            """
            SELECT * FROM scheduled_tasks
            WHERE status = 'active' AND next_run_at <= ?
            ORDER BY next_run_at ASC
            LIMIT ?
            """,
            (now_ts or store.now(), max(1, min(limit, 50))),
        )
        return [self._row_to_dict(row) for row in rows]

    def mark_running(self, task_id: int) -> bool:
        row = store.query_one("SELECT status FROM scheduled_tasks WHERE id = ?", (task_id,))
        if not row or row["status"] != "active":
            return False
        store.execute("UPDATE scheduled_tasks SET status = 'running', updated_at = ? WHERE id = ?", (store.now(), task_id))
        return True

    def complete_run(self, task: dict[str, Any], result: dict[str, Any]) -> None:
        now = store.now()
        run_count = int(task.get("run_count") or 0) + 1
        status = "active"
        next_run_at = int(task["next_run_at"])
        if task.get("schedule_type") == "daily":
            next_run_at = self._next_daily_run(task["time_of_day"], task.get("timezone") or DEFAULT_TIMEZONE, now)
        else:
            status = "completed"

        store.execute(
            """
            UPDATE scheduled_tasks
            SET status = ?, run_count = ?, last_run_at = ?, next_run_at = ?,
                last_result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, run_count, now, next_run_at, store.dumps(result), now, task["id"]),
        )

    def fail_run(self, task: dict[str, Any], result: dict[str, Any]) -> None:
        now = store.now()
        run_count = int(task.get("run_count") or 0) + 1
        next_run_at = int(task["next_run_at"])
        status = "failed"
        if task.get("schedule_type") == "daily":
            next_run_at = self._next_daily_run(task["time_of_day"], task.get("timezone") or DEFAULT_TIMEZONE, now)
            status = "active"
        store.execute(
            """
            UPDATE scheduled_tasks
            SET status = ?, run_count = ?, last_run_at = ?, next_run_at = ?,
                last_result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, run_count, now, next_run_at, store.dumps(result), now, task["id"]),
        )

    def list_for_user(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = store.query_all(
            """
            SELECT * FROM scheduled_tasks
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (LocalSessionStore._safe_user_id(user_id), max(1, min(limit, 500))),
        )
        return [self._row_to_dict(row) for row in rows]

    def recover_running(self) -> None:
        store.execute(
            "UPDATE scheduled_tasks SET status = 'active', updated_at = ? WHERE status = 'running'",
            (store.now(),),
        )

    @staticmethod
    def _next_daily_run(time_of_day: str, timezone: str, now_ts: int) -> int:
        tz = ZoneInfo(timezone or DEFAULT_TIMEZONE)
        current = datetime.fromtimestamp(now_ts, tz)
        hour, minute = (int(item) for item in (time_of_day or "09:00").split(":", 1))
        run_at = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_at <= current:
            run_at += timedelta(days=1)
        return int(run_at.timestamp())

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "session_id": row["session_id"],
            "original_request": row["original_request"],
            "task_message": row["task_message"],
            "schedule_type": row["schedule_type"],
            "time_of_day": row["time_of_day"],
            "timezone": row["timezone"],
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            "status": row["status"],
            "run_count": row["run_count"],
            "max_runs": row["max_runs"],
            "last_result": store.loads(row["last_result_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


scheduled_task_store = ScheduledTaskStore()


def _clamp_poll_seconds(value: int | None) -> int:
    if value is None:
        return 30
    return max(5, min(int(value), 3600))


class ScheduledTaskConfigStore:
    def get(self) -> dict[str, Any]:
        settings = get_settings()
        defaults = {
            "enabled": bool(settings.SCHEDULED_TASKS_ENABLED),
            "poll_seconds": _clamp_poll_seconds(settings.SCHEDULED_TASK_POLL_SECONDS),
            "timezone": DEFAULT_TIMEZONE,
        }
        row = store.query_one("SELECT value_json, updated_at FROM system_settings WHERE key = ?", (SCHEDULED_TASK_CONFIG_KEY,))
        if not row:
            return {**defaults, "updated_at": None}
        saved = store.loads(row["value_json"], {})
        return {
            "enabled": bool(saved.get("enabled", defaults["enabled"])),
            "poll_seconds": _clamp_poll_seconds(saved.get("poll_seconds", defaults["poll_seconds"])),
            "timezone": str(saved.get("timezone") or defaults["timezone"]),
            "updated_at": row["updated_at"],
        }

    def update(self, *, enabled: bool | None = None, poll_seconds: int | None = None) -> dict[str, Any]:
        current = self.get()
        next_value = {
            "enabled": current["enabled"] if enabled is None else bool(enabled),
            "poll_seconds": current["poll_seconds"] if poll_seconds is None else _clamp_poll_seconds(poll_seconds),
            "timezone": current["timezone"],
        }
        now = store.now()
        store.execute(
            """
            INSERT INTO system_settings(key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (SCHEDULED_TASK_CONFIG_KEY, store.dumps(next_value), now),
        )
        return {**next_value, "updated_at": now}

    def enabled(self) -> bool:
        return bool(self.get()["enabled"])


scheduled_task_config_store = ScheduledTaskConfigStore()


class ScheduledTaskRunner:
    def __init__(self, interval_seconds: int = 30) -> None:
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        scheduled_task_store.recover_running()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            await self._task

    async def _loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            await self.run_due_once()
            config = scheduled_task_config_store.get()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=config.get("poll_seconds") or self.interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def run_due_once(self) -> None:
        if not scheduled_task_config_store.enabled():
            return
        for task in scheduled_task_store.due_tasks():
            if not scheduled_task_store.mark_running(int(task["id"])):
                continue
            await self._run_task(task)

    async def _run_task(self, task: dict[str, Any]) -> None:
        skill = LarkCLISkill()
        session = session_store.get_or_create(task["user_id"], task["session_id"])
        context = SkillContext(
            session_id=task["session_id"],
            user_id=task["user_id"],
            message=task["task_message"],
            history=session_store.history_for_context(session),
            metadata={"scheduled_task_id": task["id"], "account_name": task["user_id"]},
        )
        try:
            result = await skill.execute(context, query=task["task_message"], confirm_write=True)
            payload = {
                "success": result.success,
                "message": result.message,
                "data": result.data or {},
            }
            session_store.append_message(task["user_id"], task["session_id"], "user", f"[定时任务] {task['task_message']}")
            session_store.append_message(task["user_id"], task["session_id"], "assistant", result.message, result.data or {})
            execution_record_store.add(
                user_id=task["user_id"],
                session_id=task["session_id"],
                request=task["task_message"],
                plan=(result.data or {}).get("plan") or {"scheduled_task_id": task["id"]},
                executed_commands=(result.data or {}).get("executed_commands") or [],
                success=result.success,
            )
            if result.success:
                scheduled_task_store.complete_run(task, payload)
            else:
                scheduled_task_store.fail_run(task, payload)
        except Exception as exc:
            scheduled_task_store.fail_run(task, {"success": False, "message": str(exc)})


scheduled_task_runner = ScheduledTaskRunner()
