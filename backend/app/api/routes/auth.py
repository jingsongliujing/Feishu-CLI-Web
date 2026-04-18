from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

ROOT_DIR = Path(__file__).resolve().parents[4]
AUTH_SESSION_PATH = ROOT_DIR / ".auth_sessions.json"
AUTH_ACCOUNT_PATH = ROOT_DIR / ".auth_accounts.json"

DEFAULT_ACCOUNTS = [
    {"account": "admin123", "name": "admin123", "password": "000000"},
    {"account": "admin", "name": "admin", "password": "000000"},
]

AUTH_TOKENS: dict[str, dict[str, str]] = {}


class LoginRequest(BaseModel):
    account: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AccountInfo(BaseModel):
    account: str
    name: str


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _read_json(path: Path, fallback: object) -> object:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_tokens() -> None:
    if AUTH_TOKENS:
        return
    payload = _read_json(AUTH_SESSION_PATH, {})
    if isinstance(payload, dict):
        AUTH_TOKENS.update({str(key): value for key, value in payload.items() if isinstance(value, dict)})


def _save_tokens() -> None:
    _write_json(AUTH_SESSION_PATH, AUTH_TOKENS)


def _load_accounts() -> list[dict[str, str]]:
    payload = _read_json(AUTH_ACCOUNT_PATH, [])
    if not isinstance(payload, list) or not payload:
        accounts = [
            {"account": item["account"], "name": item["name"], "password_hash": hash_password(item["password"])}
            for item in DEFAULT_ACCOUNTS
        ]
        _write_json(AUTH_ACCOUNT_PATH, accounts)
        return accounts
    return [item for item in payload if isinstance(item, dict)]


def _normalize_account(account: str) -> str:
    return (account or "").strip()


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
    _load_tokens()
    token = _extract_token(authorization, x_auth_token)
    if not token:
        return None
    session = AUTH_TOKENS.get(token)
    if not session:
        return None
    return AccountInfo(account=session["account"], name=session.get("name") or session["account"])


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
    _load_tokens()
    account_name = _normalize_account(request.account)
    accounts = _load_accounts()
    matched = next((item for item in accounts if str(item.get("account") or "") == account_name), None)
    if not matched or matched.get("password_hash") != hash_password(request.password):
        raise HTTPException(status_code=401, detail="账号或密码错误")

    token = secrets.token_urlsafe(32)
    AUTH_TOKENS[token] = {
        "account": account_name,
        "name": str(matched.get("name") or account_name),
        "issued_at": datetime.now().isoformat(),
    }
    _save_tokens()
    return {
        "code": 0,
        "data": {
            "token": token,
            "account": {
                "account": account_name,
                "name": str(matched.get("name") or account_name),
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
    _load_tokens()
    token = _extract_token(authorization, x_auth_token)
    if token:
        AUTH_TOKENS.pop(token, None)
        _save_tokens()
    return {"code": 0, "message": "ok"}
