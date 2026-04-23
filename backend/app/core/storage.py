from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable


ROOT_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT_DIR / ".feishu_cli_data"
DB_PATH = DATA_DIR / "feishu_cli_web.sqlite3"
LARK_CLI_PROFILES_DIR = DATA_DIR / "lark_cli_profiles"
LARK_CLI_USERS_DIR = DATA_DIR / "lark_cli_users"


class SQLiteStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LARK_CLI_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        LARK_CLI_USERS_DIR.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    name TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(account) REFERENCES accounts(account) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(session_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(session_id, user_id)
                        REFERENCES chat_sessions(session_id, user_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated
                    ON chat_sessions(user_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_chat_messages_session
                    ON chat_messages(user_id, session_id, created_at ASC);

                CREATE TABLE IF NOT EXISTS execution_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    request TEXT NOT NULL,
                    plan_json TEXT NOT NULL DEFAULT '{}',
                    executed_commands_json TEXT NOT NULL DEFAULT '[]',
                    success INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_execution_records_session
                    ON execution_records(user_id, session_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS profile_states (
                    profile TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL DEFAULT '{}',
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    original_request TEXT NOT NULL,
                    task_message TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    time_of_day TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    next_run_at INTEGER NOT NULL,
                    last_run_at INTEGER,
                    status TEXT NOT NULL DEFAULT 'active',
                    run_count INTEGER NOT NULL DEFAULT 0,
                    max_runs INTEGER,
                    last_result_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
                    ON scheduled_tasks(status, next_run_at);

                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_user
                    ON scheduled_tasks(user_id, created_at DESC);
                """
            )

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)

    @staticmethod
    def loads(value: str | None, fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except Exception:
            return fallback

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(sql, tuple(params))

    def execute_insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock, self.connect() as conn:
            cursor = conn.execute(sql, tuple(params))
            return int(cursor.lastrowid)

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock, self.connect() as conn:
            return conn.execute(sql, tuple(params)).fetchone()

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock, self.connect() as conn:
            return list(conn.execute(sql, tuple(params)).fetchall())

    def now(self) -> int:
        return int(time.time())


store = SQLiteStore()
