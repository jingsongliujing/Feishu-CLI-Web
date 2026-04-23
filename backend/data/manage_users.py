from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT_DIR / ".feishu_cli_data" / "feishu_cli_web.sqlite3"
DEFAULT_CLI_USERS_DIR = ROOT_DIR / ".feishu_cli_data" / "lark_cli_users"


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def profile_for_user(user_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", (user_id or "").strip()).strip("-").lower()
    if not normalized:
        normalized = "user"
    digest = hashlib.sha1((user_id or "user").encode("utf-8")).hexdigest()[:8]
    return f"feishu-cli-web-{normalized[:24]}-{digest}"


def ensure_schema(conn: sqlite3.Connection) -> None:
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
        """
    )


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def resolve_input_path(value: str) -> Path:
    path = Path(value)
    if path.exists():
        return path
    for candidate in (SCRIPT_DIR / value, ROOT_DIR / value):
        if candidate.exists():
            return candidate
    return path


def load_users(path: Path) -> list[dict[str, str]]:
    payload = load_json(path)
    users = payload.get("users", payload) if isinstance(payload, dict) else payload
    if not isinstance(users, list):
        raise ValueError(f"{path} must contain a users array or a JSON array")

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(users, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"user #{index} in {path} must be an object")
        account = str(item.get("account") or "").strip()
        password = str(item.get("password") or "").strip()
        name = str(item.get("name") or account).strip()
        if not account:
            raise ValueError(f"user #{index} in {path} is missing account")
        if not password:
            raise ValueError(f"user {account} in {path} is missing password")
        normalized.append({"account": account, "name": name or account, "password": password})
    return normalized


def load_accounts(path: Path) -> list[str]:
    payload = load_json(path)
    if isinstance(payload, dict):
        values = payload.get("accounts")
        if values is None and "users" in payload:
            values = [item.get("account") for item in payload["users"] if isinstance(item, dict)]
    else:
        values = payload

    if not isinstance(values, list):
        raise ValueError(f"{path} must contain an accounts array or a JSON array")

    accounts = [str(value or "").strip() for value in values]
    return [account for account in accounts if account]


def upsert_users(conn: sqlite3.Connection, users: list[dict[str, str]], dry_run: bool) -> None:
    now = int(time.time())
    for user in users:
        if dry_run:
            print(f"[dry-run] upsert account={user['account']} name={user['name']}")
            continue
        conn.execute(
            """
            INSERT INTO accounts(account, name, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account) DO UPDATE SET
                name = excluded.name,
                password_hash = excluded.password_hash,
                updated_at = excluded.updated_at
            """,
            (user["account"], user["name"], hash_password(user["password"]), now, now),
        )
        print(f"upserted account={user['account']} name={user['name']}")


def delete_users(conn: sqlite3.Connection, accounts: list[str], dry_run: bool, purge_cli_data: bool) -> None:
    for account in accounts:
        profile = profile_for_user(account)
        cli_home = DEFAULT_CLI_USERS_DIR / profile
        if dry_run:
            print(f"[dry-run] delete account={account} profile={profile}")
            if purge_cli_data:
                print(f"[dry-run] remove CLI home={cli_home}")
            continue

        conn.execute("DELETE FROM auth_sessions WHERE account = ?", (account,))
        conn.execute("DELETE FROM chat_messages WHERE user_id = ?", (account,))
        conn.execute("DELETE FROM chat_sessions WHERE user_id = ?", (account,))
        conn.execute("DELETE FROM execution_records WHERE user_id = ?", (account,))
        conn.execute("DELETE FROM scheduled_tasks WHERE user_id = ?", (account,))
        conn.execute("DELETE FROM profile_states WHERE profile = ?", (profile,))
        conn.execute("DELETE FROM accounts WHERE account = ?", (account,))

        if purge_cli_data and cli_home.exists():
            shutil.rmtree(cli_home)

        print(f"deleted account={account} profile={profile}")


def list_users(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT account, name, created_at, updated_at FROM accounts ORDER BY account").fetchall()
    if not rows:
        print("no accounts")
        return
    for row in rows:
        print(f"{row['account']}\t{row['name']}\tupdated_at={row['updated_at']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage Feishu CLI Web login accounts in SQLite.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--add-file", help="JSON file containing users to add or update.")
    parser.add_argument("--delete-file", help="JSON file containing accounts to delete.")
    parser.add_argument("--list", action="store_true", help="List existing accounts.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without writing.")
    parser.add_argument(
        "--purge-cli-data",
        action="store_true",
        help="When deleting users, also remove their isolated lark-cli home directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not args.add_file and not args.delete_file and not args.list:
        print("nothing to do; use --add-file, --delete-file, or --list")
        return 2

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(conn)

        if args.add_file:
            upsert_users(conn, load_users(resolve_input_path(args.add_file)), args.dry_run)
        if args.delete_file:
            delete_users(conn, load_accounts(resolve_input_path(args.delete_file)), args.dry_run, args.purge_cli_data)
        if args.list:
            list_users(conn)

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
