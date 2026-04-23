from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.routes.auth import AccountInfo, get_current_account
from app.core.local_sessions import LocalSessionStore
from app.core.scheduled_tasks import scheduled_task_config_store, scheduled_task_store
from app.core.storage import store

router = APIRouter()


class ScheduledTaskConfigRequest(BaseModel):
    enabled: bool | None = None
    poll_seconds: int | None = Field(default=None, ge=5, le=3600)


@router.get("/scheduled-tasks/config")
async def get_scheduled_task_config(
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    return {"code": 0, "data": scheduled_task_config_store.get()}


@router.post("/scheduled-tasks/config")
async def update_scheduled_task_config(
    request: ScheduledTaskConfigRequest,
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    return {
        "code": 0,
        "data": scheduled_task_config_store.update(
            enabled=request.enabled,
            poll_seconds=request.poll_seconds,
        ),
    }


@router.get("/scheduled-tasks")
async def list_scheduled_tasks(
    limit: int = Query(200, ge=1, le=500),
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    return {"code": 0, "data": scheduled_task_store.list_for_user(account.account, limit)}


@router.post("/scheduled-tasks/{task_id}/pause")
async def pause_scheduled_task(
    task_id: int,
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    row = store.query_one("SELECT user_id, status FROM scheduled_tasks WHERE id = ?", (task_id,))
    if not row or row["user_id"] != LocalSessionStore._safe_user_id(account.account):
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    if row["status"] != "active":
        raise HTTPException(status_code=409, detail="Only active scheduled tasks can be closed")
    store.execute(
        "UPDATE scheduled_tasks SET status = 'paused', updated_at = ? WHERE id = ?",
        (store.now(), task_id),
    )
    return {"code": 0, "message": "paused"}


@router.post("/scheduled-tasks/{task_id}/resume")
async def resume_scheduled_task(
    task_id: int,
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    row = store.query_one("SELECT user_id, status FROM scheduled_tasks WHERE id = ?", (task_id,))
    if not row or row["user_id"] != LocalSessionStore._safe_user_id(account.account):
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    if row["status"] != "paused":
        raise HTTPException(status_code=409, detail="Only paused scheduled tasks can be resumed")
    store.execute(
        "UPDATE scheduled_tasks SET status = 'active', updated_at = ? WHERE id = ?",
        (store.now(), task_id),
    )
    return {"code": 0, "message": "active"}


@router.delete("/scheduled-tasks/{task_id}")
async def delete_scheduled_task(
    task_id: int,
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    row = store.query_one("SELECT user_id, status FROM scheduled_tasks WHERE id = ?", (task_id,))
    if not row or row["user_id"] != LocalSessionStore._safe_user_id(account.account):
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    if row["status"] != "paused":
        raise HTTPException(status_code=409, detail="Please close the scheduled task before deleting it")
    store.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    return {"code": 0, "message": "deleted"}
