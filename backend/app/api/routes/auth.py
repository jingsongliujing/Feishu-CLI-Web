from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.core.storage import store

router = APIRouter()

DEFAULT_ACCOUNTS = [
    {"account": "admin123", "name": "admin123", "password": "000000"},
    {"account": "admin", "name": "admin", "password": "000000"},
    {"account": "local", "name": "local", "password": "000000"},
]


class LoginRequest(BaseModel):
    account: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AccountInfo(BaseModel):
    account: str
    name: str


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _normalize_account(account: str) -> str:
    return (account or "").strip()


def ensure_default_accounts() -> None:
    row = store.query_one("SELECT COUNT(*) AS count FROM accounts")
    if row and int(row["count"] or 0) > 0:
        return

    now = store.now()
    for item in DEFAULT_ACCOUNTS:
        store.execute(
            """
            INSERT INTO accounts(account, name, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (item["account"], item["name"], hash_password(item["password"]), now, now),
        )


def _extract_token(authorization: Optional[str], x_auth_token: Optional[str]) -> str:
    if x_auth_token:
        return x_auth_token.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def get_current_account_optional(
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None),
) -> Optional[AccountInfo]:
    ensure_default_accounts()
    token = _extract_token(authorization, x_auth_token)
    if not token:
        return None
    row = store.query_one(
        "SELECT account, name FROM auth_sessions WHERE token = ?",
        (token,),
    )
    if not row:
        return None
    return AccountInfo(account=row["account"], name=row["name"] or row["account"])


def get_current_account(
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None),
) -> AccountInfo:
    account = get_current_account_optional(authorization, x_auth_token)
    if not account:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return account


@router.post("/auth/login")
async def login(request: LoginRequest) -> dict:
    ensure_default_accounts()
    account_name = _normalize_account(request.account)
    row = store.query_one(
        "SELECT account, name, password_hash FROM accounts WHERE account = ?",
        (account_name,),
    )
    if not row or row["password_hash"] != hash_password(request.password):
        raise HTTPException(status_code=401, detail="Invalid account or password")

    token = secrets.token_urlsafe(32)
    issued_at = datetime.now().isoformat()
    store.execute(
        """
        INSERT INTO auth_sessions(token, account, name, issued_at, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (token, row["account"], row["name"] or row["account"], issued_at, store.now()),
    )
    return {
        "code": 0,
        "data": {
            "token": token,
            "account": {
                "account": row["account"],
                "name": row["name"] or row["account"],
            },
        },
    }


@router.get("/auth/me")
async def me(account: AccountInfo = Depends(get_current_account)):
    return {"code": 0, "data": account.model_dump()}


@router.post("/auth/logout")
async def logout(
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None),
) -> dict:
    token = _extract_token(authorization, x_auth_token)
    if token:
        store.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
    return {"code": 0, "message": "ok"}
