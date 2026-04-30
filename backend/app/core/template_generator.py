from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from anthropic import Anthropic
from openai import OpenAI

from app.config import get_settings


def _extract_json_payload(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _normalize_key(label: str, index: int) -> str:
    candidates = {
        "群": "group_name",
        "妙记": "minutes_url",
        "文档": "doc_url",
        "会议": "meeting_name",
        "时间": "time_window",
        "日期": "date",
        "人员": "people",
        "联系人": "contact",
        "路径": "local_path",
        "关键词": "keyword",
        "标题": "title",
        "内容": "content",
    }
    for hint, key in candidates.items():
        if hint in label:
            return key
    return f"field_{index}"


def fallback_template(requirement: str) -> dict[str, Any]:
    text = requirement.strip() or "执行一个常用飞书流程"
    labels = []
    for pattern, label in [
        (r"【([^】]+)】", None),
        (r"\[([^\]]+)\]", None),
    ]:
        labels.extend(match.group(1).strip() for match in re.finditer(pattern, text) if match.group(1).strip())

    if not labels:
        labels = ["目标对象", "补充要求"]

    fields = []
    prompt = text
    for index, label in enumerate(labels, start=1):
        key = _normalize_key(label, index)
        fields.append({"key": key, "label": label, "placeholder": f"请输入{label}"})
        prompt = prompt.replace(f"【{label}】", "{{" + key + "}}").replace(f"[{label}]", "{{" + key + "}}")

    return {
        "title": text[:24],
        "category": "自定义模板",
        "description": "由 AI 根据你的描述生成的流程模板草稿，可继续修改后保存或发布。",
        "visibility": "private",
        "prompt": prompt,
        "fields": fields,
        "change_note": "AI 生成初稿",
    }


def _sanitize_template(payload: dict[str, Any], requirement: str) -> dict[str, Any]:
    fallback = fallback_template(requirement)
    fields = payload.get("fields")
    if not isinstance(fields, list):
        fields = fallback["fields"]

    clean_fields: list[dict[str, str]] = []
    for index, item in enumerate(fields, start=1):
        if not isinstance(item, dict):
            continue
        key = re.sub(r"[^a-zA-Z0-9_]", "_", str(item.get("key") or f"field_{index}")).strip("_")
        if not key:
            key = f"field_{index}"
        label = str(item.get("label") or key).strip()
        clean_fields.append(
            {
                "key": key,
                "label": label,
                "placeholder": str(item.get("placeholder") or f"请输入{label}").strip(),
            }
        )

    return {
        "title": str(payload.get("title") or fallback["title"]).strip()[:80],
        "category": str(payload.get("category") or fallback["category"]).strip()[:40],
        "description": str(payload.get("description") or fallback["description"]).strip()[:240],
        "visibility": "private",
        "prompt": str(payload.get("prompt") or fallback["prompt"]).strip(),
        "fields": clean_fields,
        "change_note": "AI 生成初稿",
    }


async def generate_template_draft(requirement: str) -> dict[str, Any]:
    settings = get_settings()
    if not requirement.strip():
        return fallback_template(requirement)

    client: Any | None = None
    if settings.LLM_PROVIDER == "anthropic" and settings.ANTHROPIC_API_KEY:
        client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    elif settings.OPENAI_API_KEY:
        client = OpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)
    if not client:
        return fallback_template(requirement)

    system_prompt = (
        "你是飞书自动化流程模板设计专家。请把用户的一句话需求转换成可发布的模板草稿。\n"
        "只返回 JSON 对象，不要 Markdown。字段：title, category, description, prompt, fields。\n"
        "fields 是数组，每项包含 key、label、placeholder。key 必须是英文、数字或下划线。\n"
        "prompt 必须使用 {{key}} 作为变量占位符，并保留用户要达成的完整流程目标。\n"
        "模板应该让普通用户能直接填字段使用，避免抽象说明。"
    )
    user_prompt = f"用户需求：{requirement}"

    def invoke() -> str:
        if settings.LLM_PROVIDER == "anthropic":
            response = client.messages.create(
                model=settings.LLM_MODEL,
                max_tokens=1200,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""

    try:
        raw = await asyncio.wait_for(asyncio.to_thread(invoke), timeout=25)
    except Exception:
        return fallback_template(requirement)

    payload = _extract_json_payload(raw)
    if not payload:
        return fallback_template(requirement)
    return _sanitize_template(payload, requirement)

