from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.config import get_settings


ROOT_DIR = Path(__file__).resolve().parents[3]
ENV_PATH = ROOT_DIR / ".env"

MODEL_PRESETS: dict[str, dict[str, str]] = {
    "qwen": {
        "label": "通义千问 / 百炼",
        "provider": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "provider": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
    "wenxin": {
        "label": "文心一言 / 千帆",
        "provider": "openai",
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-4.0-turbo-8k",
    },
    "chatgpt": {
        "label": "ChatGPT / OpenAI",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "doubao": {
        "label": "豆包 / 火山方舟",
        "provider": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-1-6-250615",
    },
    "custom": {
        "label": "OpenAI 兼容接口",
        "provider": "openai",
        "base_url": "",
        "model": "",
    },
}


class ModelConfigRequest(BaseModel):
    preset: str = Field(default="qwen")
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    provider: str = "openai"
    use_default_qwen_key: bool = False


def _read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        example = ROOT_DIR / ".env.example"
        if example.exists():
            return example.read_text(encoding="utf-8").splitlines()
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def _quote_env_value(value: str) -> str:
    if value == "":
        return ""
    if any(char in value for char in "\n\r#'\" "):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _write_env_values(values: dict[str, str]) -> None:
    lines = _read_env_lines()
    seen: set[str] = set()
    updated: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            updated.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in values:
            updated.append(f"{key}={_quote_env_value(values[key])}")
            seen.add(key)
        else:
            updated.append(line)
    for key, value in values.items():
        if key not in seen:
            updated.append(f"{key}={_quote_env_value(value)}")
    ENV_PATH.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")


def _read_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_env_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _redact_key(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "******"
    return f"{value[:4]}...{value[-4:]}"


def current_model_config() -> dict[str, Any]:
    settings = get_settings()
    return {
        "provider": settings.LLM_PROVIDER,
        "api_key_masked": _redact_key(settings.OPENAI_API_KEY or settings.ANTHROPIC_API_KEY),
        "base_url": settings.OPENAI_BASE_URL or "",
        "model": settings.LLM_MODEL,
        "env_path": str(ENV_PATH),
    }


def presets_payload() -> list[dict[str, str]]:
    return [{"id": key, **value} for key, value in MODEL_PRESETS.items()]


def apply_model_config(request: ModelConfigRequest) -> dict[str, Any]:
    preset = MODEL_PRESETS.get(request.preset, MODEL_PRESETS["custom"])
    provider = (request.provider or preset["provider"] or "openai").strip()
    existing = _read_env_values()
    api_key = request.api_key.strip()
    if not api_key:
        api_key = existing.get("OPENAI_API_KEY", "")

    base_url = request.base_url.strip() or preset["base_url"]
    model = request.model.strip() or preset["model"]

    values = {
        "LLM_PROVIDER": provider,
        "OPENAI_API_KEY": api_key,
        "OPENAI_BASE_URL": base_url,
        "LLM_MODEL": model,
    }
    _write_env_values(values)

    get_settings.cache_clear()
    return current_model_config()
