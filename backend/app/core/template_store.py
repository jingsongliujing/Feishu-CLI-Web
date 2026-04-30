from __future__ import annotations

import re
from typing import Any

from app.api.routes.auth import AccountInfo
from app.core.storage import store


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_")
    return slug or "template"


def _row_to_template(row: Any, version_row: Any | None = None) -> dict[str, Any]:
    fields = store.loads(version_row["fields_json"] if version_row else row["fields_json"], [])
    source = version_row or row
    return {
        "id": f"user_template_{row['id']}",
        "template_id": row["id"],
        "template_key": row["template_key"],
        "title": row["title"],
        "category": row["category"],
        "description": row["description"] or "",
        "visibility": row["visibility"],
        "owner": {"account": row["owner_account"], "name": row["owner_name"]},
        "current_version": int(row["current_version"]),
        "updated_at": int(row["updated_at"]),
        "published_at": row["published_at"],
        "prompt": version_row["prompt"] if version_row else row["prompt"],
        "fields": fields if isinstance(fields, list) else [],
        "requires_ai_content_generation": bool(source["requires_ai_content_generation"]),
        "content_generation_label": source["content_generation_label"] or "",
    }


def _current_template_select(where: str, params: tuple[Any, ...]) -> Any | None:
    return store.query_one(
        f"""
        SELECT t.*, v.prompt, v.fields_json, v.requires_ai_content_generation,
               v.content_generation_label, v.editor_account, v.editor_name
        FROM user_templates t
        JOIN user_template_versions v
          ON v.template_id = t.id AND v.version = t.current_version
        WHERE {where}
        """,
        params,
    )


def _can_read(row: Any, account: AccountInfo) -> bool:
    return row["visibility"] == "public" or row["owner_account"] == account.account


def _can_write(row: Any, account: AccountInfo) -> bool:
    return row["owner_account"] == account.account


def list_accessible_templates(account: AccountInfo, scope: str = "accessible") -> list[dict[str, Any]]:
    if scope == "mine":
        rows = store.query_all(
            """
            SELECT t.*, v.prompt, v.fields_json, v.requires_ai_content_generation,
                   v.content_generation_label
            FROM user_templates t
            JOIN user_template_versions v
              ON v.template_id = t.id AND v.version = t.current_version
            WHERE t.owner_account = ?
            ORDER BY t.updated_at DESC
            """,
            (account.account,),
        )
    elif scope == "community":
        rows = store.query_all(
            """
            SELECT t.*, v.prompt, v.fields_json, v.requires_ai_content_generation,
                   v.content_generation_label
            FROM user_templates t
            JOIN user_template_versions v
              ON v.template_id = t.id AND v.version = t.current_version
            WHERE t.visibility = 'public'
            ORDER BY t.updated_at DESC
            """,
        )
    else:
        rows = store.query_all(
            """
            SELECT t.*, v.prompt, v.fields_json, v.requires_ai_content_generation,
                   v.content_generation_label
            FROM user_templates t
            JOIN user_template_versions v
              ON v.template_id = t.id AND v.version = t.current_version
            WHERE t.visibility = 'public' OR t.owner_account = ?
            ORDER BY t.updated_at DESC
            """,
            (account.account,),
        )
    return [_row_to_template(row) for row in rows]


def get_template_for_render(template_id: str, account: AccountInfo) -> dict[str, Any] | None:
    if not template_id.startswith("user_template_"):
        return None
    raw_id = template_id.removeprefix("user_template_")
    if not raw_id.isdigit():
        return None
    row = _current_template_select("t.id = ?", (int(raw_id),))
    if not row or not _can_read(row, account):
        return None
    return _row_to_template(row)


def create_template(account: AccountInfo, payload: dict[str, Any]) -> dict[str, Any]:
    now = store.now()
    title = (payload.get("title") or "未命名模板").strip()
    base_key = _slug(payload.get("template_key") or title)
    template_key = f"{account.account}_{base_key}"
    suffix = 2
    while store.query_one("SELECT id FROM user_templates WHERE template_key = ?", (template_key,)):
        template_key = f"{account.account}_{base_key}_{suffix}"
        suffix += 1

    visibility = "public" if payload.get("visibility") == "public" else "private"
    template_id = store.execute_insert(
        """
        INSERT INTO user_templates(
            template_key, title, category, description, visibility,
            owner_account, owner_name, current_version, created_at, updated_at, published_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            template_key,
            title,
            (payload.get("category") or "自定义模板").strip(),
            (payload.get("description") or "").strip(),
            visibility,
            account.account,
            account.name,
            now,
            now,
            now if visibility == "public" else None,
        ),
    )
    store.execute(
        """
        INSERT INTO user_template_versions(
            template_id, version, prompt, fields_json, requires_ai_content_generation,
            content_generation_label, editor_account, editor_name, change_note, created_at
        )
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            template_id,
            payload.get("prompt") or "",
            store.dumps(payload.get("fields") or []),
            1 if payload.get("requires_ai_content_generation") else 0,
            (payload.get("content_generation_label") or "").strip(),
            account.account,
            account.name,
            payload.get("change_note") or "创建模板",
            now,
        ),
    )
    return get_template_by_numeric_id(template_id, account)


def update_template(template_id: int, account: AccountInfo, payload: dict[str, Any]) -> dict[str, Any]:
    row = _current_template_select("t.id = ?", (template_id,))
    if not row:
        raise KeyError("not_found")
    if not _can_write(row, account):
        raise PermissionError("only owner can update template")

    now = store.now()
    version = int(row["current_version"]) + 1
    visibility = "public" if payload.get("visibility", row["visibility"]) == "public" else "private"
    store.execute(
        """
        UPDATE user_templates
        SET title = ?, category = ?, description = ?, visibility = ?, current_version = ?,
            updated_at = ?, published_at = CASE WHEN ? = 'public' THEN COALESCE(published_at, ?) ELSE published_at END
        WHERE id = ?
        """,
        (
            (payload.get("title") or row["title"]).strip(),
            (payload.get("category") or row["category"]).strip(),
            (payload.get("description") or row["description"] or "").strip(),
            visibility,
            version,
            now,
            visibility,
            now,
            template_id,
        ),
    )
    store.execute(
        """
        INSERT INTO user_template_versions(
            template_id, version, prompt, fields_json, requires_ai_content_generation,
            content_generation_label, editor_account, editor_name, change_note, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            template_id,
            version,
            payload.get("prompt") if payload.get("prompt") is not None else row["prompt"],
            store.dumps(payload.get("fields") if payload.get("fields") is not None else store.loads(row["fields_json"], [])),
            1 if payload.get("requires_ai_content_generation", row["requires_ai_content_generation"]) else 0,
            (payload.get("content_generation_label") if payload.get("content_generation_label") is not None else row["content_generation_label"] or "").strip(),
            account.account,
            account.name,
            payload.get("change_note") or f"保存版本 {version}",
            now,
        ),
    )
    return get_template_by_numeric_id(template_id, account)


def get_template_by_numeric_id(template_id: int, account: AccountInfo) -> dict[str, Any]:
    row = _current_template_select("t.id = ?", (template_id,))
    if not row:
        raise KeyError("not_found")
    if not _can_read(row, account):
        raise PermissionError("no access")
    return _row_to_template(row)


def list_versions(template_id: int, account: AccountInfo) -> list[dict[str, Any]]:
    row = store.query_one("SELECT * FROM user_templates WHERE id = ?", (template_id,))
    if not row:
        raise KeyError("not_found")
    if not _can_read(row, account):
        raise PermissionError("no access")
    versions = store.query_all(
        """
        SELECT id, version, prompt, fields_json, requires_ai_content_generation,
               content_generation_label, editor_account, editor_name, change_note, created_at
        FROM user_template_versions
        WHERE template_id = ?
        ORDER BY version DESC
        """,
        (template_id,),
    )
    return [
        {
            "id": item["id"],
            "version": item["version"],
            "prompt": item["prompt"],
            "fields": store.loads(item["fields_json"], []),
            "requires_ai_content_generation": bool(item["requires_ai_content_generation"]),
            "content_generation_label": item["content_generation_label"] or "",
            "editor": {"account": item["editor_account"], "name": item["editor_name"]},
            "change_note": item["change_note"],
            "created_at": item["created_at"],
            "is_current": int(item["version"]) == int(row["current_version"]),
        }
        for item in versions
    ]


def publish_template(template_id: int, account: AccountInfo) -> dict[str, Any]:
    row = store.query_one("SELECT * FROM user_templates WHERE id = ?", (template_id,))
    if not row:
        raise KeyError("not_found")
    if not _can_write(row, account):
        raise PermissionError("only owner can publish template")
    now = store.now()
    store.execute(
        "UPDATE user_templates SET visibility = 'public', published_at = COALESCE(published_at, ?), updated_at = ? WHERE id = ?",
        (now, now, template_id),
    )
    return get_template_by_numeric_id(template_id, account)


def rollback_template(template_id: int, version: int, account: AccountInfo) -> dict[str, Any]:
    row = store.query_one("SELECT * FROM user_templates WHERE id = ?", (template_id,))
    version_row = store.query_one(
        "SELECT * FROM user_template_versions WHERE template_id = ? AND version = ?",
        (template_id, version),
    )
    if not row or not version_row:
        raise KeyError("not_found")
    if not _can_write(row, account):
        raise PermissionError("only owner can rollback template")
    return update_template(
        template_id,
        account,
        {
            "title": row["title"],
            "category": row["category"],
            "description": row["description"],
            "visibility": row["visibility"],
            "prompt": version_row["prompt"],
            "fields": store.loads(version_row["fields_json"], []),
            "requires_ai_content_generation": bool(version_row["requires_ai_content_generation"]),
            "content_generation_label": version_row["content_generation_label"] or "",
            "change_note": f"回滚到版本 {version}",
        },
    )

