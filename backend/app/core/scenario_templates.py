from __future__ import annotations

from typing import Any

from app.core.lark_workflow_templates import LARK_WORKFLOW_TEMPLATES


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


def render_template(template_id: str, values: dict[str, str]) -> str:
    template = next((item for item in SCENARIO_TEMPLATES if item["id"] == template_id), None)
    if not template:
        raise KeyError(template_id)
    prompt = str(template["prompt"])
    for field in template.get("fields", []):
        key = field["key"]
        prompt = prompt.replace("{{" + key + "}}", (values.get(key) or field.get("placeholder") or "").strip())
    return prompt
