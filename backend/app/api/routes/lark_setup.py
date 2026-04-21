import asyncio
import json
import os
import queue
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.routes.auth import AccountInfo, get_current_account
from app.skills.lark_cli.profiles import (
    auth_status_has_user,
    cli_env_for_profile,
    cli_home_for_profile,
    profile_for_user,
    save_profile_state,
)
from app.skills.lark_cli.skill import LarkCLISkill

router = APIRouter()

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
AUTH_WAIT_SECONDS = 180


@dataclass
class SetupCommandStep:
    key: str
    title: str
    command: list[str]
    display_command: str
    description: str


@dataclass
class SetupCLIState:
    user_id: str
    profile: str
    installed: bool
    configured: bool
    authenticated: bool
    install_info: str = ""
    config_info: str = ""
    auth_info: str = ""

    @property
    def ready(self) -> bool:
        return self.installed and self.configured and self.authenticated


class LarkSetupRequest(BaseModel):
    user_id: str = Field(default="local", min_length=1)
    force_full: bool = False
    force_auth: bool = False
    reinstall_skills: bool = False
    scopes: list[str] = Field(default_factory=list)


def _serialize_sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _clean_terminal_chunk(chunk: bytes) -> str:
    text = chunk.decode("utf-8", errors="ignore")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return ANSI_ESCAPE_RE.sub("", text)


def _redact_auth_output(text: str) -> str:
    if not text:
        return ""
    redacted = re.sub(r'("device_code"\s*:\s*")[^"]+(")', r"\1******\2", text)
    redacted = re.sub(r"(--device-code\s+)[^\s)]+", r"\1******", redacted)
    return re.sub(r"(device-code\s+)[^\s)]+", r"\1******", redacted)


def _find_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _collect_auth_metadata(payload: object, text: str = "") -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    url_keys = {"verification_uri_complete", "verification_url", "verification_uri", "auth_url", "url"}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for raw_key, item in value.items():
                key = str(raw_key).lower()
                if key in url_keys and isinstance(item, str) and item.startswith("http"):
                    metadata.setdefault("auth_url", item)
                elif key == "device_code" and isinstance(item, str):
                    metadata.setdefault("device_code", item)
                elif key == "user_code" and isinstance(item, str):
                    metadata.setdefault("user_code", item)
                elif key == "expires_in" and isinstance(item, (int, float, str)):
                    metadata.setdefault("expires_in", item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    if "auth_url" not in metadata:
        urls = URL_RE.findall(text)
        if urls:
            metadata["auth_url"] = urls[0]
    return metadata


def _command_name(name: str) -> str:
    return f"{name}.cmd" if os.name == "nt" else name


def _run_capture_sync(args: list[str], timeout: int = 20, profile: str = "") -> dict[str, Any]:
    env = cli_env_for_profile(profile) if profile else None
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return {"success": False, "return_code": -1, "stdout": "", "stderr": f"Command not found: {args[0]}"}
    except subprocess.TimeoutExpired as exc:
        stdout = _clean_terminal_chunk(exc.stdout or b"")
        stderr = _clean_terminal_chunk(exc.stderr or b"")
        return {
            "success": False,
            "return_code": -1,
            "stdout": stdout,
            "stderr": f"{stderr}\nCommand timed out after {timeout} seconds.".strip(),
        }

    return {
        "success": completed.returncode == 0,
        "return_code": completed.returncode,
        "stdout": _clean_terminal_chunk(completed.stdout),
        "stderr": _clean_terminal_chunk(completed.stderr),
    }


async def _run_capture(args: list[str], timeout: int = 20, profile: str = "") -> dict[str, Any]:
    return await asyncio.to_thread(_run_capture_sync, args, timeout, profile)


async def _probe_cli_state(user_id: str) -> SetupCLIState:
    profile = profile_for_user(user_id or "local")
    install_path = shutil.which("lark-cli")
    installed = bool(install_path)
    install_info = install_path or "lark-cli command was not found"
    configured = False
    authenticated = False
    config_info = f"User profile has not initialized Lark CLI: {profile}"
    auth_info = "User profile has not completed Lark authorization."

    if installed:
        config_result = await _run_capture([_command_name("lark-cli"), "config", "show"], timeout=8, profile=profile)
        configured = bool(config_result["success"])
        config_info = str(config_result["stdout"] or config_result["stderr"] or config_info).strip()
        save_profile_state(profile, {"configured": configured})

        auth_result = await _run_capture([_command_name("lark-cli"), "auth", "status"], timeout=8, profile=profile)
        auth_info = str(auth_result["stdout"] or auth_result["stderr"] or auth_info).strip()
        authenticated = bool(auth_result["success"]) and auth_status_has_user(auth_info)
        if authenticated:
            configured = True
            save_profile_state(profile, {"configured": True, "last_auth_login": True})

    return SetupCLIState(
        user_id=user_id,
        profile=profile,
        installed=installed,
        configured=configured,
        authenticated=authenticated,
        install_info=install_info,
        config_info=config_info,
        auth_info=auth_info,
    )


def _build_setup_steps(cli_state: SetupCLIState, request: LarkSetupRequest) -> list[SetupCommandStep]:
    scopes = [scope.strip() for scope in request.scopes if scope.strip()]
    steps: list[SetupCommandStep] = []

    if request.force_full or not cli_state.installed:
        steps.append(
            SetupCommandStep(
                key="install_cli",
                title="安装飞书 CLI",
                command=[_command_name("npm"), "install", "-g", "@larksuite/cli"],
                display_command="npm install -g @larksuite/cli",
                description="当前服务器还没有检测到 lark-cli，需要先安装官方 CLI。",
            )
        )

    if request.force_full or request.reinstall_skills or not cli_state.installed:
        steps.append(
            SetupCommandStep(
                key="install_skills",
                title="安装飞书能力包",
                command=[_command_name("npx"), "skills", "add", "larksuite/cli", "-y", "-g"],
                display_command="npx skills add larksuite/cli -y -g",
                description="安装官方飞书 CLI skills，让系统能识别消息、日程、文档、多维表格等命令。",
            )
        )

    if request.force_full or not cli_state.configured:
        steps.append(
            SetupCommandStep(
                key="config_init",
                title="初始化飞书应用配置",
                command=[_command_name("lark-cli"), "config", "init", "--new"],
                display_command="lark-cli config init --new",
                description="为当前 Web 账号准备独立的飞书 CLI 配置，后续授权会写入这个隔离环境。",
            )
        )

    if request.force_full or request.force_auth or scopes or not cli_state.authenticated:
        if request.force_auth:
            steps.append(
                SetupCommandStep(
                    key="clear_auth",
                    title="退出旧授权",
                    command=[_command_name("lark-cli"), "auth", "logout"],
                    display_command="lark-cli auth logout",
                    description="先清除当前账号已有的飞书登录态，避免新旧授权混用。",
                )
            )
        auth_args = ["--scope", " ".join(scopes)] if scopes else ["--recommend"]
        auth_display = (
            f"lark-cli auth login --scope \"{' '.join(scopes)}\" --no-wait --json"
            if scopes
            else "lark-cli auth login --recommend --no-wait --json"
        )
        steps.append(
            SetupCommandStep(
                key="auth_login",
                title="打开飞书授权链接",
                command=[_command_name("lark-cli"), "auth", "login", *auth_args, "--no-wait", "--json"],
                display_command=auth_display,
                description="系统会生成一个授权链接，请在浏览器里完成登录和授权。",
            )
        )

    return steps


def _remove_auth_fields(value: object) -> object:
    auth_keys = {
        "accessToken",
        "access_token",
        "refreshToken",
        "refresh_token",
        "token",
        "tokens",
        "user",
        "users",
    }
    if isinstance(value, dict):
        return {key: _remove_auth_fields(item) for key, item in value.items() if key not in auth_keys}
    if isinstance(value, list):
        return [_remove_auth_fields(item) for item in value]
    return value


def _clear_profile_auth(profile: str) -> dict[str, Any]:
    home = cli_home_for_profile(profile)
    lark_dir = home / ".lark-cli"
    config_path = lark_dir / "config.json"
    removed: list[str] = []

    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            cleaned = _remove_auth_fields(payload)
            config_path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
            removed.append(str(config_path))
        except Exception as exc:
            return {"success": False, "message": f"Failed to clear config auth fields: {exc}", "removed": removed}

    cache_dir = lark_dir / "cache"
    if cache_dir.exists():
        for path in cache_dir.glob("auth_login*"):
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
            except Exception as exc:
                return {"success": False, "message": f"Failed to remove auth cache {path}: {exc}", "removed": removed}

    save_profile_state(profile, {"last_auth_login": False, "authenticated": False})
    return {"success": True, "message": "Previous authorization was cleared.", "removed": removed}


def _read_pipe_to_queue(pipe: object, source: str, event_queue: "queue.Queue[dict[str, Any]]") -> None:
    try:
        while True:
            chunk = pipe.readline()  # type: ignore[attr-defined]
            if not chunk:
                break
            event_queue.put({"source": source, "chunk": chunk})
    finally:
        try:
            pipe.close()  # type: ignore[attr-defined]
        except Exception:
            pass


async def _stream_popen_events(args: list[str], profile: str = "") -> AsyncGenerator[dict[str, Any], None]:
    event_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
    env = cli_env_for_profile(profile) if profile else None
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    except FileNotFoundError:
        yield {"type": "process_done", "return_code": -1, "stderr": f"Command not found: {args[0]}"}
        return

    threads: list[threading.Thread] = []
    for pipe, source in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        if pipe is None:
            continue
        thread = threading.Thread(target=_read_pipe_to_queue, args=(pipe, source, event_queue), daemon=True)
        thread.start()
        threads.append(thread)

    while process.poll() is None or any(thread.is_alive() for thread in threads) or not event_queue.empty():
        try:
            queued = await asyncio.to_thread(event_queue.get, True, 0.2)
        except queue.Empty:
            continue

        cleaned = _clean_terminal_chunk(queued["chunk"])
        if cleaned:
            yield {"type": "terminal", "stream": str(queued["source"]), "chunk": cleaned}
            for url in URL_RE.findall(cleaned):
                yield {"type": "auth", "auth_url": url}

    return_code = process.wait()
    for thread in threads:
        thread.join(timeout=0.2)
    yield {"type": "process_done", "return_code": return_code}


async def _run_auth_login_step(step: SetupCommandStep, profile: str) -> AsyncGenerator[dict[str, Any], None]:
    stdout_buffer: list[str] = []
    stderr_buffer: list[str] = []

    start_result = await _run_capture(step.command, timeout=20, profile=profile)
    stdout = str(start_result.get("stdout") or "")
    stderr = str(start_result.get("stderr") or "")
    if stdout:
        stdout_buffer.append(stdout)
        yield {"type": "terminal", "stream": "stdout", "chunk": _redact_auth_output(stdout)}
    if stderr:
        stderr_buffer.append(stderr)
        yield {"type": "terminal", "stream": "stderr", "chunk": _redact_auth_output(stderr)}

    combined = f"{stdout}\n{stderr}".strip()
    metadata = _collect_auth_metadata(_find_json_object(combined), combined)
    if metadata:
        public_metadata = {key: value for key, value in metadata.items() if key != "device_code"}
        if public_metadata:
            yield {"type": "auth", **public_metadata}

    if not start_result.get("success"):
        yield {
            "type": "step_done",
            "step_key": step.key,
            "success": False,
            "return_code": start_result.get("return_code", -1),
            "stdout": "".join(stdout_buffer)[-12000:],
            "stderr": "".join(stderr_buffer)[-12000:],
        }
        return

    device_code = metadata.get("device_code")
    if not isinstance(device_code, str) or not device_code:
        stderr_buffer.append(
            "\nNo device_code was returned by lark-cli auth login --no-wait --json. "
            "Please upgrade @larksuite/cli or run auth login in a server terminal."
        )
        yield {
            "type": "step_done",
            "step_key": step.key,
            "success": False,
            "return_code": start_result.get("return_code", -1),
            "stdout": "".join(stdout_buffer)[-12000:],
            "stderr": "".join(stderr_buffer)[-12000:],
        }
        return

    yield {
        "type": "auth_wait",
        "profile": profile,
        "message": "Authorization link created. Finish login in your browser; the server is waiting for completion.",
    }

    complete_result = await _run_capture(
        [_command_name("lark-cli"), "auth", "login", "--device-code", device_code],
        timeout=AUTH_WAIT_SECONDS,
        profile=profile,
    )
    complete_stdout = str(complete_result.get("stdout") or "")
    complete_stderr = str(complete_result.get("stderr") or "")
    if complete_stdout:
        stdout_buffer.append(complete_stdout)
        yield {"type": "terminal", "stream": "stdout", "chunk": _redact_auth_output(complete_stdout)}
    if complete_stderr:
        stderr_buffer.append(complete_stderr)
        yield {"type": "terminal", "stream": "stderr", "chunk": _redact_auth_output(complete_stderr)}

    auth_result = await _run_capture([_command_name("lark-cli"), "auth", "status"], timeout=8, profile=profile)
    auth_output = str(auth_result.get("stdout") or auth_result.get("stderr") or "")
    success = bool(auth_result.get("success")) and auth_status_has_user(auth_output)
    if success:
        save_profile_state(profile, {"configured": True, "last_auth_login": True})
    elif auth_output:
        stderr_buffer.append(f"\nLast auth status output: {auth_output[-2000:]}")

    yield {
        "type": "step_done",
        "step_key": step.key,
        "success": success,
        "return_code": complete_result.get("return_code", -1),
        "stdout": "".join(stdout_buffer)[-12000:],
        "stderr": "".join(stderr_buffer)[-12000:],
    }


async def _run_step(step: SetupCommandStep, profile: str) -> AsyncGenerator[dict[str, Any], None]:
    yield {"type": "step_start", "step": asdict(step)}

    if step.key == "clear_auth":
        logout_result = await _run_capture(step.command, timeout=20, profile=profile) if step.command else {}
        logout_output = str(logout_result.get("stdout") or logout_result.get("stderr") or "").strip()
        if logout_output:
            yield {
                "type": "terminal",
                "stream": "stdout" if logout_result.get("success") else "stderr",
                "chunk": logout_output + "\n",
            }
        result = await asyncio.to_thread(_clear_profile_auth, profile)
        yield {
            "type": "terminal",
            "stream": "stdout" if result["success"] else "stderr",
            "chunk": result["message"] + "\n",
        }
        yield {
            "type": "step_done",
            "step_key": step.key,
            "success": bool(result["success"]),
            "return_code": 0 if result["success"] else 1,
            "stdout": result["message"] if result["success"] else "",
            "stderr": "" if result["success"] else result["message"],
        }
        return

    if step.key == "auth_login":
        async for event in _run_auth_login_step(step, profile):
            yield event
        return

    stdout_buffer: list[str] = []
    stderr_buffer: list[str] = []
    return_code = -1
    async for event in _stream_popen_events(step.command, profile):
        if event.get("type") == "terminal":
            if event.get("stream") == "stdout":
                stdout_buffer.append(str(event.get("chunk") or ""))
            else:
                stderr_buffer.append(str(event.get("chunk") or ""))
            yield event
        elif event.get("type") == "auth":
            yield event
        elif event.get("type") == "process_done":
            return_code = int(event.get("return_code", -1))
            if event.get("stderr"):
                stderr_buffer.append(str(event.get("stderr")))

    success = return_code == 0
    if success and step.key == "config_init":
        save_profile_state(profile, {"configured": True, "config_init": "lark-cli config init --new"})

    yield {
        "type": "step_done",
        "step_key": step.key,
        "success": success,
        "return_code": return_code,
        "stdout": "".join(stdout_buffer)[-12000:],
        "stderr": "".join(stderr_buffer)[-12000:],
    }


def _build_status_payload(cli_state: SetupCLIState, steps: list[SetupCommandStep], guide: str = "") -> dict[str, Any]:
    return {
        "ready": cli_state.ready,
        "user_id": cli_state.user_id,
        "profile": cli_state.profile,
        "state": asdict(cli_state),
        "steps": [asdict(step) for step in steps],
        "guide": guide,
    }


@router.get("/lark/setup/status")
async def get_lark_setup_status(
    user_id: str = Query("local", min_length=1),
    account: AccountInfo = Depends(get_current_account),
) -> dict[str, Any]:
    resolved_user_id = account.account
    skill = LarkCLISkill()
    cli_state = await _probe_cli_state(resolved_user_id)
    pending_steps = _build_setup_steps(cli_state, LarkSetupRequest(user_id=resolved_user_id))
    return {"code": 0, "data": _build_status_payload(cli_state, pending_steps, skill.get_install_guide())}


@router.post("/lark/setup/stream")
async def stream_lark_setup(
    request: LarkSetupRequest,
    account: AccountInfo = Depends(get_current_account),
) -> StreamingResponse:
    request.user_id = account.account

    async def event_stream() -> AsyncGenerator[str, None]:
        skill = LarkCLISkill()
        cli_state = await _probe_cli_state(request.user_id)
        steps = _build_setup_steps(cli_state, request)

        yield _serialize_sse({"type": "status", **_build_status_payload(cli_state, steps, skill.get_install_guide())})

        if not steps:
            yield _serialize_sse(
                {
                    "type": "done",
                    "success": True,
                    "user_id": request.user_id,
                    "profile": cli_state.profile,
                    "message": "This user profile already has Lark CLI installed, configured, and authorized.",
                }
            )
            return

        for step in steps:
            async for event in _run_step(step, cli_state.profile):
                yield _serialize_sse(event)
                if event.get("type") == "step_done" and not event.get("success"):
                    yield _serialize_sse(
                        {
                            "type": "done",
                            "success": False,
                            "user_id": request.user_id,
                            "profile": cli_state.profile,
                            "message": f"{step.title} failed. Check the terminal log and retry.",
                        }
                    )
                    return

        refreshed_state = await _probe_cli_state(request.user_id)
        yield _serialize_sse(
            {
                "type": "done",
                "success": refreshed_state.ready,
                "user_id": request.user_id,
                "profile": refreshed_state.profile,
                "state": asdict(refreshed_state),
                "message": "Lark CLI setup completed." if refreshed_state.ready else "Setup finished, but some checks are still incomplete.",
            }
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
