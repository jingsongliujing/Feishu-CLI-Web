from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any


DATA_DIR = Path.cwd().parent / ".feishu_cli_data" if Path.cwd().name == "backend" else Path.cwd() / ".feishu_cli_data"
SESSIONS_DIR = DATA_DIR / "sessions"


class LocalSessionStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_user_id(user_id: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", (user_id or "local").strip()).strip("-")
        return normalized or "local"

    def _path(self, user_id: str, session_id: str) -> Path:
        user_dir = SESSIONS_DIR / self._safe_user_id(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir / f"{session_id}.json"

    def _read(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def get_or_create(self, user_id: str, session_id: str = "") -> dict[str, Any]:
        resolved_user = user_id or "local"
        resolved_session = session_id or str(uuid.uuid4())
        path = self._path(resolved_user, resolved_session)
        now = int(time.time())
        with self._lock:
            payload = self._read(path)
            if payload:
                return payload
            payload = {
                "session_id": resolved_session,
                "user_id": resolved_user,
                "title": "新会话",
                "created_at": now,
                "updated_at": now,
                "messages": [],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return payload

    def append_message(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        path = self._path(user_id or "local", session_id)
        with self._lock:
            payload = self._read(path) or self.get_or_create(user_id, session_id)
            messages = payload.setdefault("messages", [])
            messages.append(
                {
                    "id": str(uuid.uuid4()),
                    "role": role,
                    "content": content or "",
                    "metadata": metadata or {},
                    "created_at": now,
                }
            )
            if role == "user" and (payload.get("title") == "新会话" or not payload.get("title")):
                title = (content or "").strip().replace("\n", " ")
                payload["title"] = title[:28] or "新会话"
            payload["updated_at"] = now
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return payload

    def history_for_context(self, session: dict[str, Any], limit: int = 20) -> list[dict[str, str]]:
        messages = session.get("messages")
        if not isinstance(messages, list):
            return []
        history: list[dict[str, str]] = []
        for item in messages[-limit:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role in {"user", "assistant"} and content:
                history.append({"role": role, "content": content})
        return history

    def list_sessions(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        user_dir = SESSIONS_DIR / self._safe_user_id(user_id or "local")
        if not user_dir.exists():
            return []
        sessions: list[dict[str, Any]] = []
        for path in user_dir.glob("*.json"):
            payload = self._read(path)
            if not payload:
                continue
            messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
            sessions.append(
                {
                    "session_id": payload.get("session_id") or path.stem,
                    "user_id": payload.get("user_id") or user_id,
                    "title": payload.get("title") or "新会话",
                    "message_count": len(messages),
                    "created_at": payload.get("created_at") or 0,
                    "updated_at": payload.get("updated_at") or 0,
                }
            )
        sessions.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
        return sessions[: max(1, min(limit, 200))]

    def get_session(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        payload = self._read(self._path(user_id or "local", session_id))
        return payload or None

    def delete_session(self, user_id: str, session_id: str) -> bool:
        path = self._path(user_id or "local", session_id)
        with self._lock:
            if not path.exists():
                return False
            path.unlink()
            return True


session_store = LocalSessionStore()
