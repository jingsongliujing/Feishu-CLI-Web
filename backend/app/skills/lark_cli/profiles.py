from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.core.storage import LARK_CLI_PROFILES_DIR, LARK_CLI_USERS_DIR, store

PROFILE_STATE_DIR = LARK_CLI_PROFILES_DIR
CLI_USER_HOME_DIR = LARK_CLI_USERS_DIR


def profile_for_user(user_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", (user_id or "").strip()).strip("-").lower()
    if not normalized:
        normalized = "user"
    digest = hashlib.sha1((user_id or "user").encode("utf-8")).hexdigest()[:8]
    return f"feishu-cli-web-{normalized[:24]}-{digest}"


def cli_home_for_profile(profile: str) -> Path:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", (profile or "user").strip()).strip("-")
    if not normalized:
        normalized = "user"
    home = CLI_USER_HOME_DIR / normalized
    home.mkdir(parents=True, exist_ok=True)
    return home


def cli_env_for_profile(profile: str) -> Dict[str, str]:
    """Return an isolated process environment for one web login user.

    Lark CLI's official quick-start commands are intentionally used without
    `--profile`. Isolation is provided by the process HOME/USERPROFILE instead,
    so each account keeps its own CLI config and user token.
    """

    env = os.environ.copy()
    home = str(cli_home_for_profile(profile))
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["LARK_CLI_WEB_PROFILE"] = profile
    return env


def isolated_lark_cli_config_path(profile: str) -> Path:
    return cli_home_for_profile(profile) / ".lark-cli" / "config.json"


def profile_state_path(profile: str) -> Path:
    PROFILE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return PROFILE_STATE_DIR / f"{profile}.json"


def load_profile_state(profile: str) -> Dict[str, object]:
    row = store.query_one("SELECT state_json FROM profile_states WHERE profile = ?", (profile,))
    if row:
        value = store.loads(row["state_json"], {})
        return value if isinstance(value, dict) else {}
    return {}


def save_profile_state(profile: str, payload: Dict[str, object]) -> None:
    existing = load_profile_state(profile)
    existing.update(payload)
    store.execute(
        """
        INSERT INTO profile_states(profile, state_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(profile) DO UPDATE SET
            state_json = excluded.state_json,
            updated_at = excluded.updated_at
        """,
        (profile, store.dumps(existing), store.now()),
    )


def lark_cli_config_path() -> Path:
    configured = os.environ.get("LARK_CLI_CONFIG_PATH")
    if configured:
        return Path(configured)
    return Path.home() / ".lark-cli" / "config.json"


def _profile_name(app: Dict[str, object]) -> str:
    return str(app.get("name") or app.get("appId") or "").strip()


def load_lark_cli_config() -> Dict[str, object]:
    path = lark_cli_config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def list_lark_cli_apps() -> List[Dict[str, object]]:
    payload = load_lark_cli_config()
    apps = payload.get("apps")
    if not isinstance(apps, list):
        return []
    return [app for app in apps if isinstance(app, dict)]


def find_lark_cli_profile(profile: str) -> Optional[Dict[str, object]]:
    for app in list_lark_cli_apps():
        if _profile_name(app) == profile:
            return app
    return None


def _candidate_base_apps(target_profile: str) -> List[Dict[str, object]]:
    apps = list_lark_cli_apps()
    preferred_name = os.environ.get("LARK_CLI_BASE_PROFILE", "").strip()
    preferred_app_id = os.environ.get("LARK_CLI_BASE_APP_ID", "").strip()

    def is_usable(app: Dict[str, object]) -> bool:
        return bool(app.get("appId") and app.get("appSecret") and _profile_name(app) != target_profile)

    candidates = [app for app in apps if is_usable(app)]
    if preferred_name:
        named = [app for app in candidates if _profile_name(app) == preferred_name]
        if named:
            return named + [app for app in candidates if app not in named]
    if preferred_app_id:
        matched = [app for app in candidates if str(app.get("appId") or "") == preferred_app_id]
        if matched:
            return matched + [app for app in candidates if app not in matched]

    global_like = [app for app in candidates if not app.get("name")]
    if global_like:
        return global_like + [app for app in candidates if app not in global_like]
    return candidates


def ensure_user_profile_config(profile: str, app_mode: str = "auto") -> Tuple[bool, str]:
    """Ensure a per-system-user CLI profile has app credentials.

    `shared` clones the server's shared bot/app credentials into the current
    system user's profile. `custom` preserves the user's own app and lets
    `lark-cli config init --new --name <profile>` create it when missing.
    `auto` is intentionally the same as `custom`; using a shared administrator
    app must be an explicit user choice because some tenants block ordinary
    users from authorizing administrator-created CLI apps.
    """

    if not profile:
        return False, "profile is empty"

    path = lark_cli_config_path()
    if not path.exists():
        return False, f"lark-cli config not found: {path}"

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"failed to read lark-cli config: {exc}"

    apps = payload.get("apps")
    if not isinstance(apps, list):
        return False, "lark-cli config has no apps list"

    mode = (app_mode or "auto").strip().lower()
    if mode not in {"auto", "shared", "custom"}:
        mode = "auto"
    explicit_mode = mode
    state_mode = str(load_profile_state(profile).get("app_mode") or "").strip().lower()
    if mode == "auto":
        mode = "custom"

    existing_index = None
    existing_app: Optional[Dict[str, object]] = None
    for index, app in enumerate(apps):
        if isinstance(app, dict) and _profile_name(app) == profile:
            existing_index = index
            existing_app = app
            break

    if existing_app and existing_app.get("appId") and existing_app.get("appSecret"):
        if mode != "shared":
            if state_mode == "shared" and explicit_mode != "shared":
                return False, f"profile {profile} currently uses a shared app; run lark-cli config init --new --name {profile} to create this user's own app"
            return True, f"profile {profile} already has app credentials"

    if mode == "custom":
        return False, f"profile {profile} has no custom app credentials; run lark-cli config init --new --name {profile}"

    candidates = _candidate_base_apps(profile)
    base_app = candidates[0] if candidates else None

    if not base_app:
        return False, "no shared Lark CLI app profile is available; run lark-cli config init --new once as the app administrator"

    cloned: Dict[str, object] = {
        "name": profile,
        "appId": base_app.get("appId"),
        "appSecret": base_app.get("appSecret"),
        "brand": base_app.get("brand") or "feishu",
        "lang": base_app.get("lang") or "zh",
        "users": existing_app.get("users", []) if existing_app and existing_app.get("appId") == base_app.get("appId") else [],
    }
    for optional_key in ("defaultAs", "tenantKey"):
        if optional_key in base_app:
            cloned[optional_key] = base_app[optional_key]

    if existing_index is None:
        apps.append(cloned)
    else:
        apps[existing_index] = cloned

    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = path.with_suffix(path.suffix + ".bak")
    try:
        if path.exists():
            shutil.copyfile(path, backup_path)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        return False, f"failed to write lark-cli config: {exc}"

    save_profile_state(profile, {"app_mode": "shared"})
    return True, f"profile {profile} now uses shared app {base_app.get('appId')}"


def lark_profile_user_summary(profile: str) -> Dict[str, object]:
    app = find_lark_cli_profile(profile) or {}
    users = app.get("users")
    first_user = users[0] if isinstance(users, list) and users and isinstance(users[0], dict) else {}
    return {
        "profile": profile,
        "app_id": app.get("appId"),
        "user_open_id": first_user.get("userOpenId") if isinstance(first_user, dict) else None,
        "user_name": first_user.get("userName") if isinstance(first_user, dict) else None,
    }


def profile_user_mismatch(profile: str, expected_user_name: str = "") -> Tuple[bool, str]:
    expected = (expected_user_name or "").strip()
    if not expected:
        return False, ""
    summary = lark_profile_user_summary(profile)
    actual = str(summary.get("user_name") or "").strip()
    if not actual:
        return False, ""
    if actual == expected:
        return False, actual
    if re.fullmatch(r"用户\d+", actual):
        return False, actual
    return True, actual


def auth_status_has_user(output: str) -> bool:
    text = (output or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "no user logged in" in lowered or "need_user_authorization" in lowered:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "identity" in lowered and "user" in lowered
    if not isinstance(payload, dict):
        return False
    identity = str(payload.get("identity") or "").lower()
    user = payload.get("user") or payload.get("users") or payload.get("user_id") or payload.get("open_id")
    if identity == "user":
        return True
    if isinstance(user, str) and user and user != "(no logged-in users)":
        return True
    if isinstance(user, dict) and user:
        return True
    if isinstance(user, list) and len(user) > 0:
        return True
    return False
