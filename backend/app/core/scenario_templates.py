from __future__ import annotations

from typing import Any

from app.core.lark_workflow_templates import LARK_WORKFLOW_TEMPLATES

EXECUTION_GUARD = """

执行要求：
1. 先解析并校验用户填写的对象、链接、人员、群、文件路径和时间范围；如果信息仍有歧义或缺失，先向用户追问，不要猜测执行。
2. 涉及发消息、创建/修改/删除、审批、权限、上传下载、订阅监听等写操作时，先生成清晰执行计划并等待用户确认；用户确认后再执行。
3. 优先使用已配置的飞书 CLI skill 和官方能力，不要编造不存在的接口、字段或资源 ID。
4. 如果当前飞书权限、API 能力或本地文件条件不足，停止写操作，列出缺失权限/参数/文件，并给出下一步可执行方案。
5. 执行完成后返回结果链接、对象名称、关键 ID、失败项和下一步建议。
""".strip()


SCENARIO_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "send_group_notice",
        "title": "群通知",
        "category": "IM",
        "description": "向指定飞书群发送通知，适合项目同步、会议提醒、发布公告。",
        "prompt": "给飞书群「{{group}}」发送通知：{{message}}",
        "fields": [
            {"key": "group", "label": "群名称", "placeholder": "例如：飞书 CLI 测试群"},
            {"key": "message", "label": "通知内容", "placeholder": "例如：明天下午三点开项目同步会"},
        ],
    },
    {
        "id": "schedule_meeting",
        "title": "创建会议",
        "category": "Calendar",
        "description": "查找参会人空闲时间，创建日程，并可继续通知参会人或群。",
        "prompt": "帮我和 {{attendees}} 在 {{time_window}} 找一个 {{duration}} 的空闲时间，创建主题为「{{topic}}」的会议",
        "fields": [
            {"key": "attendees", "label": "参会人", "placeholder": "例如：张三、李四"},
            {"key": "time_window", "label": "时间范围", "placeholder": "例如：下周"},
            {"key": "duration", "label": "会议时长", "placeholder": "例如：1小时"},
            {"key": "topic", "label": "会议主题", "placeholder": "例如：项目复盘"},
        ],
    },
    {
        "id": "create_doc",
        "title": "生成文档",
        "category": "Doc",
        "description": "创建飞书云文档，并写入结构化内容。",
        "prompt": "创建一个名为「{{title}}」的飞书云文档，内容包括：{{content}}",
        "fields": [
            {"key": "title", "label": "文档标题", "placeholder": "例如：本周工作总结"},
            {"key": "content", "label": "文档内容", "placeholder": "例如：完成事项、风险、下周计划"},
        ],
    },
    {
        "id": "base_import",
        "title": "导入多维表格",
        "category": "Base",
        "description": "把本地 Excel/CSV 导入为飞书多维表格，并按需发送链接。",
        "prompt": "创建一个名为「{{name}}」的多维表格，并把本地文件 {{file_path}} 导入进去",
        "fields": [
            {"key": "name", "label": "表格名称", "placeholder": "例如：销售数据汇总"},
            {"key": "file_path", "label": "本地文件路径", "placeholder": "例如：./docs/sales.xlsx"},
        ],
    },
    {
        "id": "meeting_summary",
        "title": "会议纪要总结",
        "category": "Minutes",
        "description": "搜索会议纪要，生成摘要和行动项。",
        "prompt": "帮我搜索关于「{{keyword}}」的会议纪要，总结关键结论和行动项",
        "fields": [
            {"key": "keyword", "label": "关键词", "placeholder": "例如：项目复盘"},
        ],
    },
]

SCENARIO_TEMPLATES.extend(LARK_WORKFLOW_TEMPLATES)


def find_template(template_id: str) -> dict[str, Any] | None:
    return next((item for item in SCENARIO_TEMPLATES if item["id"] == template_id), None)


def missing_required_fields(template: dict[str, Any], values: dict[str, str]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for field in template.get("fields", []):
        key = str(field["key"])
        value = (values.get(key) or "").strip()
        if not value:
            missing.append(
                {
                    "key": key,
                    "label": str(field.get("label") or key),
                    "placeholder": str(field.get("placeholder") or ""),
                }
            )
    return missing


def stabilize_prompt(template: dict[str, Any], prompt: str) -> str:
    category = template.get("category") or "Scenario"
    title = template.get("title") or template.get("id") or "模板"
    return f"请执行以下飞书场景模板：{title}（分类：{category}）。\n\n{prompt.strip()}\n\n{EXECUTION_GUARD}"


def render_template(template_id: str, values: dict[str, str]) -> str:
    template = find_template(template_id)
    if not template:
        raise KeyError(template_id)
    prompt = str(template["prompt"])
    for field in template.get("fields", []):
        key = field["key"]
        prompt = prompt.replace("{{" + key + "}}", (values.get(key) or field.get("placeholder") or "").strip())
    return stabilize_prompt(template, prompt)
