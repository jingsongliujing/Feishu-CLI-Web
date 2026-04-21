from __future__ import annotations

from typing import Any

from app.core.local_sessions import LocalSessionStore
from app.core.storage import store


class ExecutionRecordStore:
    def add(
        self,
        *,
        user_id: str,
        session_id: str,
        request: str,
        plan: dict[str, Any] | None = None,
        executed_commands: list[dict[str, Any]] | None = None,
        success: bool = False,
    ) -> None:
        safe_user = LocalSessionStore._safe_user_id(user_id)
        store.execute(
            """
            INSERT INTO execution_records(
                session_id, user_id, request, plan_json, executed_commands_json, success, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                safe_user,
                request or "",
                store.dumps(plan or {}),
                store.dumps(executed_commands or []),
                1 if success else 0,
                store.now(),
            ),
        )

    def list_for_session(self, user_id: str, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        safe_user = LocalSessionStore._safe_user_id(user_id)
        rows = store.query_all(
            """
            SELECT id, request, plan_json, executed_commands_json, success, created_at
            FROM execution_records
            WHERE user_id = ? AND session_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (safe_user, session_id, max(1, min(limit, 200))),
        )
        return [
            {
                "id": row["id"],
                "request": row["request"],
                "plan": store.loads(row["plan_json"], {}),
                "executed_commands": store.loads(row["executed_commands_json"], []),
                "success": bool(row["success"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]


execution_record_store = ExecutionRecordStore()
