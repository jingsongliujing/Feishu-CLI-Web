from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.routes.auth import AccountInfo, get_current_account
from app.core.template_generator import generate_template_draft
from app.core import template_store

router = APIRouter()


class TemplateField(BaseModel):
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    placeholder: str = ""


class TemplateSaveRequest(BaseModel):
    template_key: str | None = None
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    description: str = ""
    prompt: str = Field(min_length=1)
    fields: list[TemplateField] = Field(default_factory=list)
    visibility: str = "private"
    change_note: str = ""

    def as_store_payload(self) -> dict[str, Any]:
        payload = self.model_dump()
        payload["fields"] = [item.model_dump() for item in self.fields]
        return payload


class TemplateGenerateRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=4000)


@router.get("/templates")
async def list_templates(
    scope: str = Query(default="accessible", pattern="^(accessible|mine|community)$"),
    account: AccountInfo = Depends(get_current_account),
) -> dict:
    return {"code": 0, "data": template_store.list_accessible_templates(account, scope)}


@router.post("/templates")
async def create_template(
    request: TemplateSaveRequest,
    account: AccountInfo = Depends(get_current_account),
) -> dict:
    return {"code": 0, "data": template_store.create_template(account, request.as_store_payload())}


@router.post("/templates/generate")
async def generate_template(
    request: TemplateGenerateRequest,
    _account: AccountInfo = Depends(get_current_account),
) -> dict:
    return {"code": 0, "data": await generate_template_draft(request.requirement)}


@router.put("/templates/{template_id}")
async def update_template(
    template_id: int,
    request: TemplateSaveRequest,
    account: AccountInfo = Depends(get_current_account),
) -> dict:
    try:
        data = template_store.update_template(template_id, account, request.as_store_payload())
    except KeyError:
        raise HTTPException(status_code=404, detail="Template not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return {"code": 0, "data": data}


@router.post("/templates/{template_id}/publish")
async def publish_template(
    template_id: int,
    account: AccountInfo = Depends(get_current_account),
) -> dict:
    try:
        data = template_store.publish_template(template_id, account)
    except KeyError:
        raise HTTPException(status_code=404, detail="Template not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return {"code": 0, "data": data}


@router.get("/templates/{template_id}/versions")
async def list_template_versions(
    template_id: int,
    account: AccountInfo = Depends(get_current_account),
) -> dict:
    try:
        data = template_store.list_versions(template_id, account)
    except KeyError:
        raise HTTPException(status_code=404, detail="Template not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return {"code": 0, "data": data}


@router.post("/templates/{template_id}/versions/{version}/rollback")
async def rollback_template(
    template_id: int,
    version: int,
    account: AccountInfo = Depends(get_current_account),
) -> dict:
    try:
        data = template_store.rollback_template(template_id, version, account)
    except KeyError:
        raise HTTPException(status_code=404, detail="Template or version not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return {"code": 0, "data": data}
