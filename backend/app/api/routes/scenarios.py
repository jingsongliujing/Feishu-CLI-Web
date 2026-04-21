from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.routes.auth import AccountInfo, get_current_account
from app.core.scenario_templates import SCENARIO_TEMPLATES, render_template

router = APIRouter()


class ScenarioRenderRequest(BaseModel):
    template_id: str = Field(min_length=1)
    values: dict[str, str] = Field(default_factory=dict)


@router.get("/scenarios")
async def list_scenarios(_account: AccountInfo = Depends(get_current_account)) -> dict:
    return {"code": 0, "data": SCENARIO_TEMPLATES}


@router.post("/scenarios/render")
async def render_scenario(
    request: ScenarioRenderRequest,
    _account: AccountInfo = Depends(get_current_account),
) -> dict:
    try:
        message = render_template(request.template_id, request.values)
    except KeyError:
        raise HTTPException(status_code=404, detail="Scenario template not found")
    return {"code": 0, "data": {"message": message}}
