from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.routes.auth import AccountInfo, get_current_account
from app.core.scenario_templates import SCENARIO_TEMPLATES, render_template
from app.core.template_store import get_template_for_render, list_accessible_templates

router = APIRouter()


class ScenarioRenderRequest(BaseModel):
    template_id: str = Field(min_length=1)
    values: dict[str, str] = Field(default_factory=dict)


@router.get("/scenarios")
async def list_scenarios(_account: AccountInfo = Depends(get_current_account)) -> dict:
    return {"code": 0, "data": [*SCENARIO_TEMPLATES, *list_accessible_templates(_account)]}


@router.post("/scenarios/render")
async def render_scenario(
    request: ScenarioRenderRequest,
    _account: AccountInfo = Depends(get_current_account),
) -> dict:
    try:
        user_template = get_template_for_render(request.template_id, _account)
        if user_template:
            message = render_template_from_user_template(user_template, request.values)
        else:
            message = render_template(request.template_id, request.values)
    except KeyError:
        raise HTTPException(status_code=404, detail="Scenario template not found")
    return {"code": 0, "data": {"message": message}}


def render_template_from_user_template(template: dict, values: dict[str, str]) -> str:
    prompt = str(template["prompt"])
    for field in template.get("fields", []):
        key = field["key"]
        prompt = prompt.replace("{{" + key + "}}", (values.get(key) or field.get("placeholder") or "").strip())
    return prompt
