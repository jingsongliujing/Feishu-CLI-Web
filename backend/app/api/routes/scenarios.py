from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.routes.auth import AccountInfo, get_current_account
from app.core.scenario_templates import (
    SCENARIO_TEMPLATES,
    find_template,
    missing_required_fields,
    render_template,
    stabilize_prompt,
)
from app.core.template_store import get_template_for_render, list_accessible_templates

router = APIRouter()


class ScenarioRenderRequest(BaseModel):
    template_id: str = Field(min_length=1)
    values: dict[str, str] = Field(default_factory=dict)
    enable_ai_content_generation: bool = True


@router.get("/scenarios")
async def list_scenarios(_account: AccountInfo = Depends(get_current_account)) -> dict:
    return {"code": 0, "data": [*SCENARIO_TEMPLATES, *list_accessible_templates(_account)]}


@router.post("/scenarios/render")
async def render_scenario(
    request: ScenarioRenderRequest,
    _account: AccountInfo = Depends(get_current_account),
) -> dict:
    source_template: dict | None = None
    try:
        user_template = get_template_for_render(request.template_id, _account)
        if user_template:
            source_template = user_template
            missing = missing_required_fields(user_template, request.values)
            if missing:
                return {
                    "code": 0,
                    "data": {
                        "message": "",
                        "executable": False,
                        "missing_fields": missing,
                    },
                }
            message = render_template_from_user_template(user_template, request.values)
        else:
            template = find_template(request.template_id)
            if template:
                source_template = template
                missing = missing_required_fields(template, request.values)
                if missing:
                    return {
                        "code": 0,
                        "data": {
                            "message": "",
                            "executable": False,
                            "missing_fields": missing,
                        },
                    }
            message = render_template(request.template_id, request.values)
    except KeyError:
        raise HTTPException(status_code=404, detail="Scenario template not found")
    if request.enable_ai_content_generation:
        message = append_content_generation_hint(source_template, message)
    return {"code": 0, "data": {"message": message, "executable": True, "missing_fields": []}}


def render_template_from_user_template(template: dict, values: dict[str, str]) -> str:
    prompt = str(template["prompt"])
    for field in template.get("fields", []):
        key = field["key"]
        prompt = prompt.replace("{{" + key + "}}", (values.get(key) or field.get("placeholder") or "").strip())
    return stabilize_prompt(template, prompt)


def append_content_generation_hint(template: dict | None, message: str) -> str:
    if not template or not template.get("requires_ai_content_generation"):
        return message
    label = template.get("content_generation_label") or "AI 内容生成"
    return (
        f"{message}\n\n"
        f"内容生成开关：已启用「{label}」。"
        "在执行创建命令前，必须先调用大模型把用户的短大纲扩写成完整正文、要点和布局说明；"
        "如果最终写入的文档或 Slides 只有标题、目录或占位内容，应重新生成内容后再执行。"
    )
