from __future__ import annotations

import re
import uuid
from typing import Any

from app.core.storage import store


class LocalSessionStore:
    @staticmethod
    def _safe_user_id(user_id: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", (user_id or "local").strip()).strip("-")
        return normalized or "local"

    def _messages_for(self, user_id: str, session_id: str) -> list[dict[str, Any]]:
        rows = store.query_all(
            """
            SELECT id, role, content, metadata_json, created_at
            FROM chat_messages
            WHERE user_id = ? AND session_id = ?
            ORDER BY created_at ASC
            """,
            (self._safe_user_id(user_id), session_id),
        )
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "metadata": store.loads(row["metadata_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_or_create(self, user_id: str, session_id: str = "") -> dict[str, Any]:
        resolved_user = self._safe_user_id(user_id)
        resolved_session = session_id or str(uuid.uuid4())
        row = store.query_one(
            "SELECT * FROM chat_sessions WHERE user_id = ? AND session_id = ?",
            (resolved_user, resolved_session),
        )
        if not row:
            now = store.now()
            store.execute(
                """
                INSERT INTO chat_sessions(session_id, user_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (resolved_session, resolved_user, "New chat", now, now),
            )
            row = store.query_one(
                "SELECT * FROM chat_sessions WHERE user_id = ? AND session_id = ?",
                (resolved_user, resolved_session),
            )
        return {
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "messages": self._messages_for(resolved_user, resolved_session),
        }

    def append_message(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_user = self._safe_user_id(user_id)
        self.get_or_create(resolved_user, session_id)
        now = store.now()
        store.execute(
            """
            INSERT INTO chat_messages(id, session_id, user_id, role, content, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                session_id,
                resolved_user,
                role,
                content or "",
                store.dumps(metadata or {}),
                now,
            ),
        )
        session = self.get_session(resolved_user, session_id) or {}
        title = session.get("title") or ""
        if role == "user" and title in {"", "New chat"}:
            new_title = (content or "").strip().replace("\n", " ")[:28] or "New chat"
            store.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE user_id = ? AND session_id = ?",
                (new_title, now, resolved_user, session_id),
            )
        else:
            store.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE user_id = ? AND session_id = ?",
                (now, resolved_user, session_id),
            )
        return self.get_session(resolved_user, session_id) or {}

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
        resolved_user = self._safe_user_id(user_id)
        rows = store.query_all(
            """
            SELECT s.session_id, s.user_id, s.title, s.created_at, s.updated_at, COUNT(m.id) AS message_count
            FROM chat_sessions s
            LEFT JOIN chat_messages m ON m.user_id = s.user_id AND m.session_id = s.session_id
            WHERE s.user_id = ?
            GROUP BY s.session_id, s.user_id
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (resolved_user, max(1, min(limit, 200))),
        )
        return [
            {
                "session_id": row["session_id"],
                "user_id": row["user_id"],
                "title": row["title"] or "New chat",
                "message_count": row["message_count"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def get_session(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        resolved_user = self._safe_user_id(user_id)
        row = store.query_one(
            "SELECT * FROM chat_sessions WHERE user_id = ? AND session_id = ?",
            (resolved_user, session_id),
        )
        if not row:
            return None
        return {
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "messages": self._messages_for(resolved_user, session_id),
        }

    def delete_session(self, user_id: str, session_id: str) -> bool:
        resolved_user = self._safe_user_id(user_id)
        existing = store.query_one(
            "SELECT 1 FROM chat_sessions WHERE user_id = ? AND session_id = ?",
            (resolved_user, session_id),
        )
        if not existing:
            return False
        store.execute(
            "DELETE FROM chat_sessions WHERE user_id = ? AND session_id = ?",
            (resolved_user, session_id),
        )
        return True


session_store = LocalSessionStore()
