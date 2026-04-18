"""
Runtime implementation for Lark CLI skills.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Tuple

import yaml
from anthropic import Anthropic
from openai import OpenAI

from app.config import get_settings
from app.skills.base import BaseSkill, SkillContext, SkillResult
from app.skills.lark_cli.profiles import (
    auth_status_has_user,
    cli_env_for_profile,
    isolated_lark_cli_config_path,
    profile_for_user,
)

MAX_LARK_CLI_STEPS = 10
DEFAULT_LARK_LLM_TIMEOUT = 25
DEFAULT_LARK_COMMAND_TIMEOUT = 30

SKILL_KEYWORD_HINTS: Dict[str, Tuple[str, ...]] = {
    "lark-reliable-scenes": (
        "飞书CLI测试群",
        "赵鹤翔",
        "杨继涛",
        "黄云",
        "刘劲松",
        "今天的十条AI新闻",
        "今天的销售数据汇总",
        "抖店-京东热店排名",
        "大家都合适的时间",
        "会议号发在群里",
        "分别发送给他们",
        "高成功率",
    ),
    "lark-doc": ("文档", "云文档", "doc", "docs", "docx", "知识文档"),
    "lark-im": ("消息", "发消息", "发送消息", "聊天", "群聊", "通知", "reply", "message"),
    "lark-contact": ("联系人", "用户", "同事", "人员", "search user", "search-user", "查人"),
    "lark-calendar": ("日历", "会议", "日程", "agenda", "空闲", "安排会议"),
    "lark-wiki": ("wiki", "知识库", "知识空间", "节点"),
    "lark-drive": ("云空间", "文件", "文件夹", "drive", "上传附件", "下载附件"),
    "lark-sheets": ("表格", "电子表格", "sheet", "sheets", "单元格"),
    "lark-base": ("多维表格", "bitable", "base", "记录", "字段", "仪表盘"),
    "lark-task": ("任务", "待办", "task", "tasklist"),
    "lark-mail": ("邮件", "mail", "邮箱", "草稿"),
    "lark-slides": ("幻灯片", "slides", "演示文稿", "ppt"),
    "lark-whiteboard": ("白板", "画板", "whiteboard"),
    "lark-vc": ("视频会议", "会议纪要", "录制", "vc"),
}


WRITE_KEYWORDS = (
    "send",
    "reply",
    "create",
    "update",
    "delete",
    "remove",
    "upload",
    "download",
    "insert",
    "append",
    "move",
    "import",
    "export",
    "forward",
    "schedule",
    "invite",
    "add",
    "edit",
    "rename",
    "write",
    "发消息",
    "发个消息",
    "告诉",
    "发消息",
    "发送",
    "回复",
    "创建",
    "新建",
    "修改",
    "更新",
    "删除",
    "上传",
    "下载",
    "插入",
    "追加",
    "移动",
    "导入",
    "导出",
    "转发",
    "邀请",
    "添加",
    "编辑",
    "写入",
)

CN_NUMBER_MAP = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass
class ReferenceDoc:
    path: Path
    title: str
    content: str


@dataclass
class SkillDoc:
    key: str
    name: str
    version: str
    description: str
    cli_help: str
    requires: Dict[str, Any]
    file_path: Path
    content: str
    references: List[ReferenceDoc]


@dataclass
class PlannedStep:
    command: str
    reason: str
    expected: str


@dataclass
class LarkCLIState:
    installed: bool
    configured: bool
    authenticated: bool
    profile: str = ""
    user_id: str = ""
    install_info: str = ""
    config_info: str = ""
    auth_info: str = ""
    authorized_user_name: str = ""
    auth_user_mismatch: bool = False

    @property
    def ready(self) -> bool:
        return self.installed and self.configured and self.authenticated


@dataclass
class NormalizedIntent:
    normalized_query: str
    intent_type: str = ""


class LarkCLISkill(BaseSkill):
    def __init__(self) -> None:
        self.settings = get_settings()
        self._skills_dir = Path(__file__).resolve().parent / "skills"
        self._skills_metadata: Dict[str, SkillDoc] = {}
        self._load_skills_metadata()
        self._init_llm_client()

    def _init_llm_client(self) -> None:
        self.client: Optional[Any] = None
        if self.settings.LLM_PROVIDER == "anthropic" and self.settings.ANTHROPIC_API_KEY:
            self.client = Anthropic(api_key=self.settings.ANTHROPIC_API_KEY)
            return
        if self.settings.OPENAI_API_KEY:
            self.client = OpenAI(
                api_key=self.settings.OPENAI_API_KEY,
                base_url=self.settings.OPENAI_BASE_URL,
            )

    @property
    def _llm_timeout(self) -> int:
        timeout_value = getattr(self.settings, "LARK_CLI_LLM_TIMEOUT", None)
        if isinstance(timeout_value, int) and timeout_value > 0:
            return timeout_value
        return DEFAULT_LARK_LLM_TIMEOUT

    def _load_skills_metadata(self) -> None:
        if not self._skills_dir.exists():
            return

        for skill_path in sorted(self._skills_dir.iterdir()):
            if not skill_path.is_dir():
                continue

            skill_md_path = skill_path / "SKILL.md"
            if not skill_md_path.exists():
                continue

            try:
                content = skill_md_path.read_text(encoding="utf-8")
                frontmatter, body = self._split_frontmatter(content)
                metadata = frontmatter if isinstance(frontmatter, dict) else {}
                references = self._load_reference_docs(skill_path)
                self._skills_metadata[skill_path.name] = SkillDoc(
                    key=skill_path.name,
                    name=str(metadata.get("name", skill_path.name)),
                    version=str(metadata.get("version", "1.0.0")),
                    description=str(metadata.get("description", "")),
                    cli_help=str(metadata.get("metadata", {}).get("cliHelp", "")),
                    requires=metadata.get("metadata", {}).get("requires", {}) or {},
                    file_path=skill_md_path,
                    content=body.strip(),
                    references=references,
                )
            except Exception as exc:
                print(f"Warning: failed to load skill metadata from {skill_md_path}: {exc}")

    def _load_reference_docs(self, skill_path: Path) -> List[ReferenceDoc]:
        refs_dir = skill_path / "references"
        if not refs_dir.exists():
            return []

        docs: List[ReferenceDoc] = []
        for ref_path in sorted(refs_dir.glob("*.md")):
            try:
                content = ref_path.read_text(encoding="utf-8")
                title = self._extract_title(content) or ref_path.stem
                docs.append(ReferenceDoc(path=ref_path, title=title, content=content.strip()))
            except Exception as exc:
                print(f"Warning: failed to load reference {ref_path}: {exc}")
        return docs

    @staticmethod
    def _split_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
        if not content.startswith("---"):
            return {}, content
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}, content
        try:
            frontmatter = yaml.safe_load(parts[1]) or {}
        except Exception:
            frontmatter = {}
        return frontmatter, parts[2]

    @staticmethod
    def _extract_title(content: str) -> str:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
        return ""

    @property
    def name(self) -> str:
        return "lark_cli"

    @property
    def description(self) -> str:
        return (
            "飞书/Lark CLI 智能技能。会先读取 app/skills/lark_cli/skills 下的 markdown 规则，"
            "再自动判断应该用哪个业务域、需要哪些 reference 文档、是否要先查 schema，"
            "最后规划并执行 lark-cli 命令。适用于飞书消息、文档、日历、云空间、多维表格、表格、"
            "通讯录、审批、知识库等操作。也支持直接传入完整 lark-cli 命令执行。"
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户的飞书操作需求，技能会自动做意图判断、文档选择和命令编排。",
                },
                "command": {
                    "type": "string",
                    "description": "完整的 lark-cli 命令。提供后将直接执行，不再自动规划。",
                },
                "confirm_write": {
                    "type": "boolean",
                    "description": "是否已经明确确认执行写操作。发送消息、创建、更新、删除、上传等操作需要为 true。",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "命令执行超时时间，单位秒。",
                    "default": 30,
                },
            },
        }

    def get_install_guide(self) -> str:
        return (
            "飞书 CLI 未就绪，请先完成以下步骤：\n"
            "1. `npm install -g @larksuite/cli`\n"
            "2. `npx skills add larksuite/cli -y -g`\n"
            "3. `lark-cli config init --new`\n"
            "4. `lark-cli auth login --recommend`\n"
            "5. `lark-cli auth status`\n"
        )

    def get_skill_info(self, skill_name: str) -> Dict[str, Any]:
        doc = self._skills_metadata.get(skill_name)
        if not doc:
            return {}
        return {
            "name": doc.name,
            "version": doc.version,
            "description": doc.description,
            "cli_help": doc.cli_help,
            "requires": doc.requires,
            "file_path": str(doc.file_path),
        }

    def list_all_skills(self) -> List[str]:
        return sorted(self._skills_metadata.keys())

    def get_skill_content(self, skill_name: str) -> str:
        doc = self._skills_metadata.get(skill_name)
        return doc.content if doc else ""

    @staticmethod
    def _truncate_text(text: str, limit: int = 1200) -> str:
        stripped = (text or "").strip()
        if len(stripped) <= limit:
            return stripped
        return stripped[:limit] + "\n...[truncated]"

    @staticmethod
    def _extract_json_payload(raw_text: str) -> Optional[Dict[str, Any]]:
        text = (raw_text or "").strip()
        if not text:
            return None

        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        candidates = [text]
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            candidates.append(match.group(0))

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _normalize_expected_type(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"write", "read", "schema", "search"}:
            return normalized
        return "read"

    def _extract_related_skill_keys(self, doc: SkillDoc) -> List[str]:
        candidates = set()
        for text in [doc.content, *(ref.content for ref in doc.references[:12])]:
            for matched in re.findall(r"\.\./([a-zA-Z0-9_-]+)/SKILL\.md", text):
                if matched in self._skills_metadata and matched != doc.key:
                    candidates.add(matched)
        return sorted(candidates)

    @staticmethod
    def _run_command_sync(command: str, timeout: int = 30, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            env=env,
        )

    @staticmethod
    def _user_profile(user_id: str) -> str:
        return profile_for_user(user_id)

    def _profile_flag(self, user_id: str) -> str:
        return f"--profile {self._quote_cli_arg(self._user_profile(user_id))}"

    def _with_user_profile(self, command: str, user_id: str) -> str:
        normalized = (command or "").strip()
        if not user_id or not normalized.startswith("lark-cli "):
            return normalized
        return re.sub(r"\s+--profile(?:=|\s+)(\"[^\"]+\"|'[^']+'|[^\s]+)", "", normalized).strip()

    def _cli_env_for_user(self, user_id: str = "") -> Optional[Dict[str, str]]:
        if not user_id:
            return None
        return cli_env_for_profile(self._user_profile(user_id))

    async def check_lark_cli_installed(self) -> Tuple[bool, str]:
        try:
            resolved = shutil.which("lark-cli")
            if resolved:
                return True, resolved
            lookup = "where lark-cli" if os.name == "nt" else "which lark-cli"
            result = await asyncio.to_thread(self._run_command_sync, lookup, 5)
            if result.returncode == 0:
                return True, result.stdout.strip()
            return False, result.stderr.strip() or "未找到 lark-cli 命令"
        except subprocess.TimeoutExpired:
            return False, "检查 lark-cli 安装状态超时"
        except Exception as exc:
            return False, f"检查 lark-cli 安装状态失败: {exc}"

    async def check_lark_cli_configured(self, user_id: str = "") -> Tuple[bool, str]:
        if user_id:
            profile = self._user_profile(user_id)
            try:
                command = "lark-cli config show"
                result = await asyncio.to_thread(
                    self._run_command_sync,
                    command,
                    8,
                    self._cli_env_for_user(user_id),
                )
                if result.returncode == 0:
                    return True, result.stdout.strip() or f"当前登录用户已完成飞书 CLI 初始化：{profile}"
                config_path = isolated_lark_cli_config_path(profile)
                return False, result.stderr.strip() or f"当前登录用户尚未初始化飞书 CLI：{config_path}"
            except subprocess.TimeoutExpired:
                return False, "当前登录用户飞书 CLI 初始化状态检查超时"
            except Exception as exc:
                return False, f"当前登录用户飞书 CLI 初始化状态检查失败: {exc}"

        config_paths = [
            Path.home() / ".lark-cli" / "config.json",
            Path.home() / ".config" / "lark-cli" / "config.json",
            Path("/root/.lark-cli/config.json"),
        ]

        for config_path in config_paths:
            if not config_path.exists():
                continue
            try:
                config_data = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(config_data, dict) and config_data:
                    return True, f"配置文件存在: {config_path}"
                return False, f"配置文件为空或无效: {config_path}"
            except json.JSONDecodeError:
                return False, f"配置文件 JSON 格式错误: {config_path}"
            except Exception as exc:
                return False, f"读取配置文件失败: {exc}"

        return False, "未发现 lark-cli 配置文件，请先执行 `lark-cli config init --new`"

    async def check_lark_cli_authenticated(self, user_id: str = "") -> Tuple[bool, str]:
        try:
            command = "lark-cli auth status"
            result = await asyncio.to_thread(
                self._run_command_sync,
                command,
                8,
                self._cli_env_for_user(user_id),
            )
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0 and auth_status_has_user(output):
                return True, output
            return False, output or "当前登录用户尚未完成飞书用户身份授权"
        except subprocess.TimeoutExpired:
            return False, "认证状态检查超时"
        except Exception as exc:
            return False, f"认证状态检查失败: {exc}"

    async def _probe_cli_state(self, user_id: str = "", expected_user_name: str = "") -> LarkCLIState:
        profile = self._user_profile(user_id) if user_id else ""
        installed, install_info = await self.check_lark_cli_installed()
        if not installed:
            return LarkCLIState(
                installed=False,
                configured=False,
                authenticated=False,
                profile=profile,
                user_id=user_id,
                install_info=install_info,
            )

        configured, config_info = await self.check_lark_cli_configured(user_id)
        if user_id and not configured:
            return LarkCLIState(
                installed=installed,
                configured=False,
                authenticated=False,
                profile=profile,
                user_id=user_id,
                install_info=install_info,
                config_info=config_info,
                auth_info="当前用户 profile 不存在或尚未初始化，需先完成飞书 CLI 初始化。",
            )

        authenticated, auth_info = await self.check_lark_cli_authenticated(user_id)
        auth_user_mismatch = False
        authorized_user_name = ""
        if authenticated and user_id:
            # The real isolation boundary is the per-user CLI HOME, not a Lark
            # CLI named profile. Avoid reading the server's global config here,
            # otherwise a shared administrator app can be mistaken for the
            # current web user's Feishu identity.
            authorized_user_name = expected_user_name or ""

        if authenticated and not configured:
            configured = True
            config_info = config_info or "认证状态可用，视为本地配置已完成。"

        return LarkCLIState(
            installed=installed,
            configured=configured,
            authenticated=authenticated,
            profile=profile,
            user_id=user_id,
            install_info=install_info,
            config_info=config_info,
            auth_info=auth_info,
            authorized_user_name=authorized_user_name,
            auth_user_mismatch=auth_user_mismatch,
        )

    async def execute_command(self, command: str, timeout: int = 30, user_id: str = "") -> Tuple[bool, str, str]:
        normalized = self._repair_command(command)
        normalized = self._with_user_profile(normalized, user_id)
        if not normalized:
            return False, "", "命令为空"
        if not normalized.startswith("lark-cli "):
            return False, "", "仅允许执行 lark-cli 命令"
        if self._has_unquoted_shell_control_operator(normalized):
            return False, "", "为安全起见，不支持包含管道或多命令控制符的 lark-cli 命令"

        try:
            result = await asyncio.to_thread(
                self._run_command_sync,
                normalized,
                timeout,
                self._cli_env_for_user(user_id),
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", f"命令执行超时（{timeout} 秒）"
        except Exception as exc:
            return False, "", f"命令执行异常: {exc}"

    @staticmethod
    def _is_bootstrap_command(command: str) -> bool:
        normalized = (command or "").lower()
        return any(
            keyword in normalized
            for keyword in (
                "npm install -g @larksuite/cli",
                "npx skills add larksuite/cli",
                "lark-cli config init",
                "lark-cli auth login",
                "lark-cli auth status",
            )
        )

    def _should_skip_bootstrap_command(self, command: str, cli_state: LarkCLIState) -> bool:
        normalized = (command or "").lower()
        if not self._is_bootstrap_command(normalized):
            return False
        if "config init" in normalized and cli_state.configured:
            return True
        if "auth login" in normalized and cli_state.authenticated:
            return True
        if "auth status" in normalized and cli_state.authenticated:
            return True
        if ("npm install -g @larksuite/cli" in normalized or "npx skills add larksuite/cli" in normalized) and cli_state.installed:
            return True
        return False

    @staticmethod
    def _build_cli_state_text(cli_state: LarkCLIState) -> str:
        profile_line = f"- profile: {cli_state.profile or 'default'}\n"
        return (
            "本地终端探测结果：\n"
            f"{profile_line}"
            f"- installed: {cli_state.installed} ({cli_state.install_info or 'n/a'})\n"
            f"- configured: {cli_state.configured} ({cli_state.config_info or 'n/a'})\n"
            f"- authenticated: {cli_state.authenticated} ({cli_state.auth_info or 'n/a'})\n"
            "规则：以上状态以本地终端探测为准；如果 configured/authenticated 已经是 true，"
            "就不要再规划 `lark-cli config init --new`、`lark-cli auth login`、`lark-cli auth status` 之类的初始化或登录检查步骤。"
            "实际执行时会自动使用当前登录用户的隔离 CLI 环境，不要手动追加 `--profile` 或切换到其它用户。"
        )

    def _build_setup_metadata(self, cli_state: LarkCLIState) -> Dict[str, Any]:
        setup_steps: List[Dict[str, str]] = []
        if not cli_state.installed:
            setup_steps.extend(
                [
                    {
                        "key": "install_cli",
                        "title": "安装 Lark CLI",
                        "command": "npm install -g @larksuite/cli",
                    },
                    {
                        "key": "install_skills",
                        "title": "安装 Lark AI Skills",
                        "command": "npx skills add larksuite/cli -y -g",
                    },
                ]
            )
        if not cli_state.configured:
            setup_steps.append(
                {
                    "key": "config_init",
                    "title": "创建自己的应用",
                    "command": "lark-cli config init --new",
                }
            )
        if cli_state.auth_user_mismatch:
            setup_steps.append(
                {
                    "key": "auth_logout",
                    "title": "清除旧授权",
                    "command": "lark-cli auth logout",
                }
            )
        if not cli_state.authenticated:
            setup_steps.append(
                {
                    "key": "auth_login",
                    "title": "授权登录",
                    "command": "lark-cli auth login --recommend",
                }
            )

        return {
            "setup_required": True,
            "setup_state": {
                "installed": cli_state.installed,
                "configured": cli_state.configured,
                "authenticated": cli_state.authenticated,
                "install_info": cli_state.install_info,
                "config_info": cli_state.config_info,
                "auth_info": cli_state.auth_info,
                "profile": cli_state.profile,
                "user_id": cli_state.user_id,
                "authorized_user_name": cli_state.authorized_user_name,
                "auth_user_mismatch": cli_state.auth_user_mismatch,
            },
            "setup_steps": setup_steps,
            "setup_guide": "",
        }

    def _build_scope_setup_metadata(self, cli_state: LarkCLIState, scopes: List[str]) -> Dict[str, Any]:
        unique_scopes = []
        for scope in scopes:
            cleaned = scope.strip()
            if cleaned and cleaned not in unique_scopes:
                unique_scopes.append(cleaned)
        command = f"lark-cli auth login --scope {self._quote_cli_arg(' '.join(unique_scopes))}"
        return {
            "setup_required": True,
            "setup_state": {
                "installed": cli_state.installed,
                "configured": cli_state.configured,
                "authenticated": cli_state.authenticated,
                "install_info": cli_state.install_info,
                "config_info": cli_state.config_info,
                "auth_info": cli_state.auth_info,
                "profile": cli_state.profile,
                "user_id": cli_state.user_id,
                "authorized_user_name": cli_state.authorized_user_name,
                "auth_user_mismatch": cli_state.auth_user_mismatch,
            },
            "setup_steps": [
                {
                    "key": "auth_login",
                    "title": "补充授权",
                    "command": command,
                }
            ],
            "setup_scopes": unique_scopes,
            "setup_guide": "",
        }

    @staticmethod
    def _extract_missing_scopes(output: str) -> List[str]:
        text = output or ""
        if not re.search(r"scope|permission|forbidden|unauthorized|not authorized|no permission|缺少|权限", text, re.IGNORECASE):
            return []
        scopes: List[str] = []
        for scope in re.findall(r"\b[a-z][a-z0-9_]*:[A-Za-z0-9_.:-]+\b", text):
            if scope not in scopes:
                scopes.append(scope)
        return scopes

    @staticmethod
    def _is_user_authorization_error(output: str) -> bool:
        return bool(
            re.search(
                r"need_user_authorization|no user logged in|user authorization|未登录|用户授权|登录授权",
                output or "",
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _normalize_command(command: str) -> str:
        text = (command or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        if text.startswith("lark-cli im +messages-send ") and " --text " in text:
            return re.sub(r"\r\n|\r|\n", r"\\n", text).strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[0] if lines else ""

    @staticmethod
    def _move_identity_flag_to_tail(command: str) -> str:
        match = re.match(r"^(lark-cli)\s+--as\s+(user|bot)\s+(.+)$", command.strip())
        if not match:
            return command.strip()
        _, identity, rest = match.groups()
        if re.search(r"\s--as\s+(user|bot)(?:\s|$)", rest):
            return f"lark-cli {rest}".strip()
        return f"lark-cli {rest} --as {identity}".strip()

    def _repair_command(self, command: str) -> str:
        normalized = self._normalize_command(command)
        if not normalized:
            return normalized

        repaired = self._move_identity_flag_to_tail(normalized)
        repaired = re.sub(r"^lark-cli\s+contact\s+search-user\b", "lark-cli contact +search-user", repaired)
        repaired = re.sub(r"^lark-cli\s+contact\s+users-search\b", "lark-cli contact +search-user", repaired)
        repaired = re.sub(r"^lark-cli\s+contact\s+get-user\b", "lark-cli contact +get-user", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+chat-create\b", "lark-cli im +chat-create", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+\+search-chat\b", "lark-cli im +chat-search", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+search-chat\b", "lark-cli im +chat-search", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+chat\s+search\b", "lark-cli im +chat-search", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+chat-search\b", "lark-cli im +chat-search", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+chats\s+list\b", "lark-cli im +chat-search", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+\+user-search\b", "lark-cli contact +search-user", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+user-search\b", "lark-cli contact +search-user", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+\+chat-message-send\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+chat-message-send\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+\+send-dm\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+send-dm\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+\+message\.send_as_user\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+message\.send_as_user\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+users-search\b", "lark-cli contact +search-user", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+messages-search\b", "lark-cli im +messages-search", repaired)
        repaired = re.sub(r"^lark-cli\s+im\s+messages-send\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+message\s+\+send\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+message\s+send\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+messages\s+\+send\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+messages\s+send\b", "lark-cli im +messages-send", repaired)
        repaired = re.sub(r"^lark-cli\s+base\s+create\b", "lark-cli base +base-create", repaired)
        repaired = re.sub(r"^lark-cli\s+base\s+base-create\b", "lark-cli base +base-create", repaired)
        repaired = re.sub(r"^lark-cli\s+docs\s+create\b", "lark-cli docs +create", repaired)
        repaired = re.sub(r"^lark-cli\s+docs\s+fetch\b", "lark-cli docs +fetch", repaired)
        repaired = re.sub(r"^lark-cli\s+docs\s+update\b", "lark-cli docs +update", repaired)
        repaired = re.sub(r"^lark-cli\s+calendar\s+\+create\b", "lark-cli calendar +create", repaired)
        repaired = re.sub(r"^lark-cli\s+drive\s+import\b", "lark-cli drive +import", repaired)
        repaired = re.sub(r"^lark-cli\s+drive\s+task_result\b", "lark-cli drive +task_result", repaired)

        if repaired.startswith("lark-cli im chat.members get "):
            chat_match = re.search(r"\boc_[A-Za-z0-9_-]+\b", repaired)
            if chat_match:
                params = json.dumps(
                    {
                        "chat_id": chat_match.group(0),
                        "member_id_type": "open_id",
                        "page_size": 50,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                return (
                    "lark-cli im chat.members get "
                    f"--params {self._quote_cli_arg(params)} --as user --format json"
                )

        if repaired.startswith("lark-cli base +base-create "):
            repaired = repaired.replace(" --title ", " --name ")
        if repaired.startswith("lark-cli drive +import "):
            repaired = repaired.replace(" --title ", " --name ")
            repaired = repaired.replace(" --path ", " --file ")
        if repaired.startswith("lark-cli im +chat-search "):
            repaired = repaired.replace(" --name ", " --query ")
            repaired = repaired.replace(" --keyword ", " --query ")
            repaired = re.sub(r"\s+--type\s+\S+", "", repaired)
        if repaired.startswith("lark-cli contact +search-user "):
            repaired = repaired.replace(" --name ", " --query ")
            repaired = repaired.replace(" --keyword ", " --query ")
        if repaired.startswith("lark-cli im +messages-send "):
            repaired = repaired.replace(" --receiver_id ", " --user-id ")
            repaired = repaired.replace(" --receiver_open_id ", " --user-id ")
            repaired = re.sub(r"\s+--receiver_type\s+\S+", "", repaired)
            repaired = repaired.replace(" --msg_type ", " --msg-type ")
            msg_type_match = re.search(r'\s--msg-type\s+(?:"([^"]+)"|(\S+))', repaired)
            msg_type = (msg_type_match.group(1) or msg_type_match.group(2) or "").lower() if msg_type_match else ""
            has_content = bool(re.search(r'\s--content\s+', repaired))
            content_is_json = False
            content_match = re.search(r'--content\s+"((?:\\"|[^"])*)"', repaired)
            if content_match:
                try:
                    json.loads(content_match.group(1).replace('\\"', '"'))
                    content_is_json = True
                except json.JSONDecodeError:
                    content_is_json = False
            if has_content and msg_type in {"interactive", "post", "share_chat", "share_user"}:
                repaired = re.sub(r'\s--msg-type\s+(?:"[^"]+"|\S+)', "", repaired)
            elif has_content and not msg_type and content_is_json:
                pass
            else:
                repaired = repaired.replace(" --content ", " --text ")
            text_match = re.search(r'--text\s+"((?:\\"|[^"])*)"', repaired)
            if text_match:
                raw_text = text_match.group(1).replace('\\"', '"')
                try:
                    payload = json.loads(raw_text)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                    repaired = repaired.replace(
                        text_match.group(0),
                        f'--text {self._quote_lark_text_arg(payload["text"])}',
                        1,
                    )
                else:
                    repaired = repaired.replace(
                        text_match.group(0),
                        f"--text {self._quote_lark_text_arg(raw_text)}",
                        1,
                    )
        if repaired.startswith("lark-cli docs +create "):
            repaired = repaired.replace(" --content ", " --markdown ")
        if repaired.startswith("lark-cli docs +fetch "):
            repaired = repaired.replace(" --doc_id ", " --doc ")
        if repaired.startswith("lark-cli calendar +create "):
            repaired = repaired.replace(" --title ", " --summary ")
        if repaired.startswith("lark-cli calendar +agenda ") and " --date " in repaired:
            date_match = re.search(r'\s--date\s+(?:"([^"]+)"|(\S+))', repaired)
            if date_match:
                raw_date = (date_match.group(1) or date_match.group(2) or "").strip()
                target_date = None
                if raw_date.lower() in {"tomorrow", "明天"}:
                    target_date = date.today() + timedelta(days=1)
                elif raw_date.lower() in {"today", "今天"}:
                    target_date = date.today()
                else:
                    try:
                        target_date = date.fromisoformat(raw_date[:10])
                    except ValueError:
                        target_date = None
                repaired = re.sub(r'\s--date\s+(?:"[^"]+"|\S+)', "", repaired)
                if target_date and " --start " not in repaired:
                    start, end = self._day_iso_bounds(target_date)
                    repaired += f" --start {self._quote_cli_arg(start)}"
                    if " --end " not in repaired:
                        repaired += f" --end {self._quote_cli_arg(end)}"

        if repaired.startswith("lark-cli contact +search-user ") and "--as " not in repaired:
            repaired += " --as user"

        if repaired.startswith("lark-cli im +messages-search ") and "--as " not in repaired:
            repaired += " --as user"

        if repaired.startswith("lark-cli task +get-my-tasks") and "--as " not in repaired:
            repaired += " --as user"

        return repaired.strip()

    @staticmethod
    def _sanitize_query_text(query: str) -> str:
        text = (query or "").strip()
        text = re.sub(r"(?:^|\s|[，,;；])confirm_write\s*=\s*true(?:\s|$|[，,;；])", " ", text, flags=re.IGNORECASE)
        return re.sub(r"\s{2,}", " ", text).strip(" ，,;；")

    @staticmethod
    def _quote_cli_arg(value: str) -> str:
        escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\r\n", "\\n").replace("\n", "\\n")
        return f'"{escaped}"'

    @staticmethod
    def _clean_lark_text(value: str) -> str:
        text = str(value or "")
        text = text.replace("\\r\\n", "；").replace("\\n", "；").replace("\\r", "；")
        text = text.replace("\r\n", "；").replace("\n", "；").replace("\r", "；")
        text = re.sub(r"\s*；\s*", "；", text)
        text = re.sub(r"；{2,}", "；", text)
        return text.strip(" ；")

    @classmethod
    def _quote_lark_text_arg(cls, value: str) -> str:
        return cls._quote_cli_arg(cls._clean_lark_text(value))

    @staticmethod
    def _has_unquoted_shell_control_operator(command: str) -> bool:
        in_single = False
        in_double = False
        escaped = False
        for char in command or "":
            if escaped:
                escaped = False
                continue
            if char == "\\" and in_double:
                escaped = True
                continue
            if char == "'" and not in_double:
                in_single = not in_single
                continue
            if char == '"' and not in_single:
                in_double = not in_double
                continue
            if char in ";&|" and not in_single and not in_double:
                return True
        return False

    @staticmethod
    def _query_requires_user_auth(query: str, command: Optional[str] = None) -> bool:
        text = f"{query or ''} {command or ''}"
        personal_tokens = (
            "日历",
            "忙闲",
            "空闲",
            "会议号",
            "私信",
            "私聊",
            "发消息",
            "发送消息",
            "消息",
            "联系人",
            "通讯录",
            "邮箱",
            "邮件",
            "contact",
            "message",
            "messages-send",
            "search-user",
            "calendar",
            "mail",
            "agenda",
            "freebusy",
            "suggestion",
        )
        if any(token in text for token in personal_tokens):
            return True
        if re.search(r"\s--as\s+user(?:\s|$)", text):
            return True
        return False

    def _build_doc_create_plan(self, query: str) -> Optional[Dict[str, Any]]:
        normalized = self._sanitize_query_text(query)
        if self._parse_bitable_import_request(normalized):
            return None
        title_match = None
        title_patterns = (
            r"(?:文档(?:名字|名称|标题)?(?:叫|叫做|是)|文档名(?:叫|为)|标题(?:叫|是)|名为)\s*[“\"'《]?(?P<title>[^”\"'》\n:：，,]+)[”\"'》]?",
            r"(?:创建|新建)(?:一个|一篇|一份)?\s*(?:名为)?\s*[“\"'《]?(?P<title>[^”\"'》\n:：，,]+)[”\"'》]?\s*的?(?:飞书)?(?:云)?文档",
            r"[《“\"'](?P<title>[^》”\"']+)[》”\"']",
            r"(?:创建|新建)(?:一个)?(?:飞书)?(?:云)?文档\s*[:：]?\s*(?P<title>[^\n:：]+?)(?:\s*(?:[:：]|内容[是为]?)\s*|$)",
        )
        for pattern in title_patterns:
            title_match = re.search(pattern, normalized)
            if title_match and title_match.group("title").strip():
                break

        content_match = re.search(r"(?:内容(?:是|为)?|正文|写入|写上|写成)\s*(.+)$", normalized)
        if "文档" not in normalized and "docs" not in normalized.lower() and "doc" not in normalized.lower():
            return None
        if not any(keyword in normalized for keyword in ("创建", "新建", "生成")):
            return None
        if not title_match:
            return None

        title = title_match.group("title").strip()
        title = re.sub(r"^(?:一个|一篇|一份)", "", title).strip(" '\"，,。；;")
        if not content_match:
            trailing_text = normalized[title_match.end():]
            trailing_match = re.search(r"^[：:]\s*(.+)$", trailing_text)
            if trailing_match:
                content_match = trailing_match
        markdown = content_match.group(1).strip() if content_match else ""
        placeholder_markdown = f"# {title}\n\n（内容待填充）"
        if markdown in {"写入其中", "写到其中", "写进去", "写入文档中", "写入这个文档里", "写入其中。"}:
            markdown = ""
        if not markdown and re.search(r"(?:写到|写入).*(?:文档|这个文档|其中)里?", normalized):
            markdown = placeholder_markdown
        if not markdown and "内容写入其中" in normalized:
            markdown = placeholder_markdown
        if not markdown:
            markdown = placeholder_markdown
        if markdown == title:
            markdown = placeholder_markdown
        if markdown.startswith("写入"):
            markdown = placeholder_markdown
        if not markdown and re.search(r"(?:写到|写入).*(?:文档|这个文档)里", normalized):
            markdown = f"# {title}\n\n（内容待填充）"
        if not title:
            return None

        command = (
            f"lark-cli docs +create --title {self._quote_cli_arg(title)} "
            f"--markdown {self._quote_cli_arg(markdown)}"
        )

        return {
            "summary": f"创建标题为《{title}》的飞书云文档" + (f"，内容为'{markdown}'" if markdown else ""),
            "relevant_skills": ["lark-doc", "lark-shared"],
            "references": ["lark-doc-create.md"],
            "need_confirmation": True,
            "reason_for_confirmation": "该请求会在飞书中新建文档，属于写操作。",
            "commands": [
                {
                    "command": command,
                    "reason": "按 lark-doc 的 +create shortcut 创建文档。",
                    "expected": "write",
                }
            ],
            "final_response_hint": "总结文档是否创建成功，并返回 doc_id、doc_url 等关键结果。",
        }

    def _build_base_create_plan(self, query: str) -> Optional[Dict[str, Any]]:
        normalized = self._sanitize_query_text(query)
        if not any(keyword in normalized for keyword in ("多维表格", "base", "bitable")):
            return None
        if self._parse_bitable_import_request(normalized):
            return None
        if not any(keyword in normalized for keyword in ("创建", "新建", "生成")):
            return None

        title_match = None
        for pattern in (
            r"(?:名为|名字叫|名称为|标题为)\s*[“\"'《]?(?P<title>[^”\"'》\n:：，,]+)[”\"'》]?",
            r"(?:创建|新建)(?:一个|一份)?\s*[“\"'《]?(?P<title>[^”\"'》\n:：，,]+)[”\"'》]?\s*的?多维表格",
        ):
            title_match = re.search(pattern, normalized)
            if title_match:
                break
        if not title_match:
            return None

        title = title_match.group("title").strip(" “”\"'《》")
        if not title:
            return None

        source_hint = ""
        source_match = re.search(r"(?:并将|并把)\s*(?P<source>.+?)\s*(?:写入|导入|放入)(?:其中|里面|到表格里)", normalized)
        if source_match:
            source_hint = source_match.group("source").strip()

        command = f"lark-cli base +base-create --name {self._quote_cli_arg(title)} --as user"
        summary = f"创建名为《{title}》的多维表格"
        if source_hint:
            summary += f"，后续准备写入：{source_hint}"

        return {
            "summary": summary,
            "relevant_skills": ["lark-base", "lark-shared"],
            "references": ["lark-base-base-create.md"],
            "need_confirmation": True,
            "reason_for_confirmation": "该请求会创建飞书多维表格，属于写操作。",
            "commands": [
                {
                    "command": command,
                    "reason": "先创建 Base 实例，后续再根据内容来源补表结构和记录写入。",
                    "expected": "write",
                }
            ],
            "final_response_hint": "总结 Base 是否创建成功，并返回 base_token、URL 等关键结果。",
        }

    @staticmethod
    def _parse_bitable_import_request(query: str) -> Optional[Dict[str, str]]:
        normalized = LarkCLISkill._sanitize_query_text(query)
        if not any(keyword in normalized for keyword in ("多维表格", "bitable", "base")):
            return None
        if not any(keyword in normalized for keyword in ("上传", "导入", "转换", "转为", "创建")):
            return None

        file_match = re.search(
            r"(?P<file>(?:\.{1,2}[\\/]|[A-Za-z]:[\\/]|/)?[^\s，,。；;\"'“”《》]+?\.(?:xlsx|xls|csv))",
            normalized,
            flags=re.IGNORECASE,
        )
        if not file_match:
            return None
        file_path = file_match.group("file").strip()
        file_path = re.sub(r"^(?:并将|并把|将|把)", "", file_path).strip()

        title = ""
        for pattern in (
            r"(?:名为|名字叫|名称为|标题为)\s*[“\"'《]?(?P<title>[^”\"'》\n:：，,]+)[”\"'》]?\s*的?多维表格",
            r"(?:创建|新建)(?:一个|一份)?\s*(?:名为)?\s*[“\"'《]?(?P<title>[^”\"'》\n:：，,]+)[”\"'》]?\s*的?多维表格",
            r"(?:上传|导入).+?[“\"'《](?P<title>[^”\"'》\n:：，,]+)[”\"'》]",
        ):
            title_match = re.search(pattern, normalized)
            if title_match:
                title = title_match.group("title").strip(" “”\"'《》")
                break
        if not title:
            title = Path(file_path).stem
        if not title:
            return None

        group = ""
        for pattern in (
            r"(?:把|将).*(?:发到|发送到|发给|发送给|同步到|通知到|分享到|转发到)\s*[【“\"'](?P<group>[^】”\"'\n，,。；;]+)[】”\"']",
            r"(?:把|将).*(?:发到|发送到|发给|发送给|同步到|通知到|分享到|转发到)\s*(?P<group>[^，,。；;\n]+群)",
            r"(?:发到|发送到|发给|发送给|同步到|通知到|分享到|转发到)\s*[【“\"'](?P<group>[^】”\"'\n，,。；;]+)[】”\"']",
            r"(?:发到|发送到|发给|发送给|同步到|通知到|分享到|转发到)\s*(?P<group>[^，,。；;\n]+群)",
        ):
            group_match = re.search(pattern, normalized)
            if group_match:
                group = group_match.group("group").strip(" 【】“”\"'")
                break

        parsed = {"title": title, "file_path": file_path}
        if group:
            parsed["group"] = group
        return parsed

    def _build_bitable_import_plan(self, query: str) -> Optional[Dict[str, Any]]:
        parsed = self._parse_bitable_import_request(query)
        if not parsed:
            return None

        title = parsed["title"]
        file_path = parsed["file_path"]
        group = parsed.get("group")
        skills = ["lark-drive", "lark-base", "lark-shared"]
        references = ["lark-drive-import.md", "lark-base-base-create.md"]
        summary = f"将本地文件 {file_path} 导入为多维表格《{title}》"
        final_hint = "总结多维表格是否导入成功，并返回链接、token 等关键结果。"
        if group:
            skills.insert(2, "lark-im")
            references.extend(["lark-im-chat-search.md", "lark-im-messages-send.md"])
            summary += f"，并发送到群【{group}】"
            final_hint = "总结多维表格导入和群通知是否都成功。"

        return {
            "summary": summary,
            "relevant_skills": skills,
            "references": references,
            "need_confirmation": True,
            "reason_for_confirmation": "该请求会上传本地文件、创建飞书多维表格并可能发送群消息，属于写操作。",
            "commands": [
                {
                    "command": (
                        f"lark-cli drive +import --file {self._quote_cli_arg(file_path)} "
                        f"--type bitable --name {self._quote_cli_arg(title)} --as user"
                    ),
                    "reason": "本地 Excel/CSV 导入为多维表格应使用 drive +import，并指定 type=bitable。",
                    "expected": "write",
                }
            ],
            "final_response_hint": final_hint,
        }

    @staticmethod
    def _parse_direct_message_request(query: str) -> Optional[Tuple[str, str]]:
        normalized = LarkCLISkill._sanitize_query_text(query)
        patterns = (
            r"给(?P<target>(?!【)[^：:\n]+?)发(?:个)?(?:飞书)?(?:私聊)?消息[：:]\s*(?P<message>.+)$",
            r"(?:帮我)?发(?:个)?(?:飞书)?消息给(?P<target>(?!【)[^：:\n]+?)[：:]\s*(?P<message>.+)$",
            r"给(?P<target>(?!【)[^：:\n]+?)发(?:个)?[：:]\s*(?P<message>.+)$",
            r"(?:帮我)?发(?:个)?给(?P<target>(?!【)[^：:\n]+?)[：:]\s*(?P<message>.+)$",
            r"(?:帮我)?私聊(?P<target>(?!【)[^：:\n]+?)[：:]\s*(?P<message>.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if not match:
                continue
            target = match.group("target").strip()
            message = match.group("message").strip()
            target = target.strip("“”\"' ")
            if target and message:
                return target, message
        return None

    @staticmethod
    def _split_people_names(raw: str) -> List[str]:
        text = (raw or "").strip()
        text = re.sub(r"(都)?发送消息$", "", text)
        text = re.sub(r"(都)?发消息$", "", text)
        text = re.sub(r"(都)?发个消息$", "", text)
        text = re.sub(r"(都)?发个$", "", text)
        text = text.replace("和", "、").replace("，", "、").replace(",", "、")
        text = re.sub(r"\s+", "", text)
        candidates = [item.strip("“”\"' ") for item in text.split("、")]
        deduped: List[str] = []
        for item in candidates:
            item = re.sub(r"(都)?发送消息$", "", item)
            item = re.sub(r"(都)?发消息$", "", item)
            item = re.sub(r"(都)?发个消息$", "", item)
            item = re.sub(r"(都)?发个$", "", item)
            if not item or item in {"我", "我自己"}:
                continue
            if item not in deduped:
                deduped.append(item)
        return deduped

    @classmethod
    def _parse_multi_direct_message_request(cls, query: str) -> Optional[Tuple[List[str], str]]:
        normalized = cls._sanitize_query_text(query)
        patterns = (
            r"给(?P<targets>[^：:\n]+?)发(?:个)?(?:飞书)?(?:私聊)?消息[：:]\s*(?P<message>.+)$",
            r"(?:帮我)?发(?:个)?(?:飞书)?消息给(?P<targets>[^：:\n]+?)[：:]\s*(?P<message>.+)$",
            r"给(?P<targets>[^：:\n]+?)发(?:个)?[：:]\s*(?P<message>.+)$",
            r"(?:帮我)?发(?:个)?给(?P<targets>[^：:\n]+?)[：:]\s*(?P<message>.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if not match:
                continue
            targets = cls._split_people_names(match.group("targets"))
            message = match.group("message").strip()
            if len(targets) >= 2 and message:
                return targets, message
        return None

    @staticmethod
    def _extract_json_candidates(text: str) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        parsed = LarkCLISkill._extract_json_payload(text)
        if parsed:
            candidates.append(parsed)
        for matched in re.findall(r"\{[\s\S]*?\}", text or ""):
            try:
                item = json.loads(matched)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                candidates.append(item)
        return candidates

    def _extract_open_id_from_output(self, text: str) -> Optional[str]:
        match = re.search(r"\bou_[A-Za-z0-9_-]+\b", text or "")
        if match:
            return match.group(0)

        for payload in self._extract_json_candidates(text):
            users = payload.get("users")
            if not users and isinstance(payload.get("data"), dict):
                users = payload["data"].get("users")
            if isinstance(users, list):
                for user in users:
                    if isinstance(user, dict) and user.get("open_id"):
                        return str(user["open_id"])
        return None

    def _extract_chat_id_from_output(self, text: str) -> Optional[str]:
        match = re.search(r"\boc_[A-Za-z0-9_-]+\b", text or "")
        if match:
            return match.group(0)

        for payload in self._extract_json_candidates(text):
            chats = payload.get("chats")
            if not chats and isinstance(payload.get("data"), dict):
                chats = payload["data"].get("chats")
            if isinstance(chats, list):
                for chat in chats:
                    if isinstance(chat, dict) and chat.get("chat_id"):
                        return str(chat["chat_id"])
        return None

    @staticmethod
    def _extract_event_id_from_output(text: str) -> Optional[str]:
        match = re.search(r'"event_id"\s*:\s*"([^"]+)"', text or "")
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_meeting_url_from_output(text: str) -> Optional[str]:
        match = re.search(r'"meeting_url"\s*:\s*"([^"]+)"', text or "")
        if match:
            return match.group(1).replace("\\u0026", "&")
        match = re.search(r"https?://(?:vc|meetings)\.feishu\.cn/[^\s\"'<>）)]+", text or "")
        if match:
            return match.group(0)
        return None

    @staticmethod
    def _extract_meeting_number_from_output(text: str) -> Optional[str]:
        meeting_url = LarkCLISkill._extract_meeting_url_from_output(text)
        if meeting_url:
            match = re.search(r"/j/(\d+)", meeting_url)
            if match:
                return match.group(1)
        match = re.search(r'"meeting_no"\s*:\s*"?(?P<number>\d{6,})"?', text or "")
        if match:
            return match.group("number")
        return None

    @staticmethod
    def _extract_url_from_output(text: str) -> Optional[str]:
        match = re.search(r"https?://[^\s\"'<>）)]+", text or "")
        if match:
            return match.group(0).replace("\\u0026", "&")
        return None

    @staticmethod
    def _extract_token_from_output(text: str) -> Optional[str]:
        patterns = (
            r'"(?:base_token|app_token|token|obj_token|file_token)"\s*:\s*"([^"]+)"',
            r"\b(?:bascn|base|bitable|app)[A-Za-z0-9_-]{6,}\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text or "")
            if match:
                return match.group(1) if match.lastindex else match.group(0)
        return None

    @staticmethod
    def _extract_next_command_from_output(text: str) -> Optional[str]:
        match = re.search(r'"next_command"\s*:\s*"([^"]+)"', text or "")
        if match:
            return match.group(1).replace('\\"', '"')
        match = re.search(r"(lark-cli\s+drive\s+\+task_result\s+--scenario\s+import\s+--ticket\s+\S+)", text or "")
        if match:
            return match.group(1).strip()
        return None

    @classmethod
    def _extract_suggestion_range(cls, text: str) -> Optional[Tuple[str, str]]:
        def visit(value: Any) -> Optional[Tuple[str, str]]:
            if isinstance(value, dict):
                start_value = (
                    value.get("start")
                    or value.get("start_time")
                    or value.get("startTime")
                    or value.get("begin_time")
                )
                end_value = (
                    value.get("end")
                    or value.get("end_time")
                    or value.get("endTime")
                    or value.get("finish_time")
                )
                if isinstance(start_value, str) and isinstance(end_value, str):
                    return start_value, end_value
                for item in value.values():
                    found = visit(item)
                    if found:
                        return found
            if isinstance(value, list):
                for item in value:
                    found = visit(item)
                    if found:
                        return found
            return None

        for payload in cls._extract_json_candidates(text):
            found = visit(payload)
            if found:
                return found

        iso_times = re.findall(r"20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2}|Z)?", text or "")
        if len(iso_times) >= 2:
            return iso_times[0], iso_times[1]
        return None

    @staticmethod
    def _build_meeting_card_content(
        *,
        title: str,
        group: str,
        start: str,
        end: str,
        event_id: str,
        event_url: Optional[str],
    ) -> str:
        detail_lines = [
            f"**{title}已创建**",
            f"时间：{start} - {end}",
            f"参与人：{group} 全员",
        ]
        if event_url:
            detail_lines.append(f"[打开日程]({event_url})")
        elif event_id:
            detail_lines.append(f"日程 ID：{event_id}")
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": title,
                }
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "\n".join(detail_lines),
                    },
                }
            ],
        }
        return json.dumps(card, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _build_meeting_card_markdown(
        *,
        title: str,
        group: str,
        start: str,
        end: str,
        event_id: str,
        event_url: Optional[str],
        meeting_url: Optional[str] = None,
        meeting_number: Optional[str] = None,
    ) -> str:
        lines = [
            f"## {title}已创建",
            f"- 时间：{start} - {end}",
            f"- 参与人：{group} 全员",
        ]
        if meeting_number:
            lines.append(f"- 会议号：{meeting_number}")
        if meeting_url:
            lines.append(f"- 会议链接：{meeting_url}")
        if event_url:
            lines.append(f"- 日程链接：{event_url}")
        elif event_id:
            lines.append(f"- 日程 ID：{event_id}")
        return "；".join(lines)

    @staticmethod
    def _build_meeting_plain_text(
        *,
        title: str,
        group: str,
        start: str,
        end: str,
        event_id: str,
        event_url: Optional[str],
        meeting_url: Optional[str] = None,
        meeting_number: Optional[str] = None,
    ) -> str:
        lines = [
            f"{title}已创建",
            f"时间：{start} - {end}",
            f"参与人：{group} 全员",
        ]
        if meeting_number:
            lines.append(f"会议号：{meeting_number}")
        if meeting_url:
            lines.append(f"会议链接：{meeting_url}")
        if event_url:
            lines.append(f"日程链接：{event_url}")
        elif event_id:
            lines.append(f"日程 ID：{event_id}")
        return "\n".join(lines)

    @staticmethod
    def _today_range() -> Tuple[str, str]:
        today = date.today()
        return today.isoformat(), today.isoformat()

    @staticmethod
    def _day_iso_bounds(target: date) -> Tuple[str, str]:
        tz = datetime.now().astimezone().tzinfo
        start_dt = datetime.combine(target, time(hour=0, minute=0), tzinfo=tz)
        end_dt = datetime.combine(target, time(hour=23, minute=59, second=59), tzinfo=tz)
        return start_dt.isoformat(timespec="seconds"), end_dt.isoformat(timespec="seconds")

    @staticmethod
    def _workday_iso_bounds(target: date) -> Tuple[str, str]:
        tz = datetime.now().astimezone().tzinfo
        start_dt = datetime.combine(target, time(hour=9, minute=0), tzinfo=tz)
        end_dt = datetime.combine(target, time(hour=18, minute=0), tzinfo=tz)
        return start_dt.isoformat(timespec="seconds"), end_dt.isoformat(timespec="seconds")

    @staticmethod
    def _iso_to_unix_seconds(value: str) -> str:
        normalized = (value or "").strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return str(int(parsed.timestamp()))

    @staticmethod
    def _extract_explicit_date(text: str) -> Optional[date]:
        normalized = LarkCLISkill._sanitize_query_text(text)
        today = date.today()
        patterns = (
            r"(?P<year>20\d{2})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*(?:日|号)?",
            r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*(?:日|号)",
            r"(?P<year>20\d{2})[/-](?P<month>\d{1,2})[/-](?P<day>\d{1,2})",
            r"(?P<month>\d{1,2})\s*[/-]\s*(?P<day>\d{1,2})\s*(?:日|号)?",
            r"(?P<month>\d{1,2})\s*[.．]\s*(?P<day>\d{1,2})\s*(?:日|号)?",
        )
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if not match:
                continue
            year = int(match.groupdict().get("year") or today.year)
            month = int(match.group("month"))
            day = int(match.group("day"))
            try:
                return date(year, month, day)
            except ValueError:
                return None
        return None

    @staticmethod
    def _this_week_range() -> Tuple[str, str]:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        return monday.isoformat(), sunday.isoformat()

    @staticmethod
    def _next_week_work_bounds() -> Tuple[str, str]:
        today = date.today()
        next_monday = today + timedelta(days=(7 - today.weekday()))
        next_friday = next_monday + timedelta(days=4)
        tz = datetime.now().astimezone().tzinfo
        start_dt = datetime.combine(next_monday, time(hour=9, minute=0), tzinfo=tz)
        end_dt = datetime.combine(next_friday, time(hour=18, minute=0), tzinfo=tz)
        return start_dt.isoformat(timespec="seconds"), end_dt.isoformat(timespec="seconds")

    @staticmethod
    def _parse_group_schedule_request(query: str) -> Optional[Dict[str, str]]:
        normalized = LarkCLISkill._sanitize_query_text(query)
        if "群" not in normalized:
            return None
        explicit_date = LarkCLISkill._extract_explicit_date(normalized)
        if not explicit_date and not any(token in normalized for token in ("下周", "本周", "这周", "明天", "今天")):
            return None
        if not any(token in normalized for token in ("所有人", "大家", "群里", "群成员")):
            return None
        if not any(token in normalized for token in ("日历", "忙闲", "合适", "空闲", "共同可用")):
            return None
        if not any(token in normalized for token in ("开", "创建", "安排", "找一个", "找个", "约")):
            return None

        group = ""
        for pattern in (
            r"【(?P<group>[^】]+)】",
            r"[“\"'](?P<group>[^”\"']+群)[”\"']",
            r"(?P<group>[^，,。；;\s]+群)",
        ):
            match = re.search(pattern, normalized)
            if match:
                group = match.group("group").strip()
                break
        if not group:
            return None

        duration_match = re.search(r"(?P<hours>\d+(?:\.\d+)?)\s*小时", normalized)
        duration_minutes = 60
        if duration_match:
            duration_minutes = max(1, int(float(duration_match.group("hours")) * 60))
        elif "半小时" in normalized:
            duration_minutes = 30

        summary = "讨论会" if "讨论" in normalized else "会议"
        if "评审" in normalized:
            summary = "需求评审会"

        return {
            "group": group,
            "summary": summary,
            "duration_minutes": str(duration_minutes),
            "date": explicit_date.isoformat() if explicit_date else "",
        }

    @staticmethod
    def _parse_people_schedule_request(query: str) -> Optional[Dict[str, str]]:
        normalized = LarkCLISkill._sanitize_query_text(query)
        if "群" in normalized:
            return None
        if not any(token in normalized for token in ("合适", "空闲", "忙闲", "共同可用", "大家都")):
            return None
        if not any(token in normalized for token in ("开会", "开个会", "会议", "讨论会", "日程")):
            return None
        attendees = LarkCLISkill._parse_attendees(normalized)
        if len(attendees) < 2:
            return None

        explicit_date = LarkCLISkill._extract_explicit_date(normalized)
        if not explicit_date and not any(token in normalized for token in ("下周", "本周", "这周", "明天", "今天")):
            return None

        duration_match = re.search(r"(?P<hours>\d+(?:\.\d+)?)\s*小时", normalized)
        duration_minutes = 60
        if duration_match:
            duration_minutes = max(1, int(float(duration_match.group("hours")) * 60))
        elif "半小时" in normalized:
            duration_minutes = 30

        summary = "讨论会" if "讨论" in normalized else "会议"
        if "评审" in normalized:
            summary = "需求评审会"

        return {
            "attendees": json.dumps(attendees, ensure_ascii=False, separators=(",", ":")),
            "summary": summary,
            "duration_minutes": str(duration_minutes),
            "date": explicit_date.isoformat() if explicit_date else "",
            "notify_each": "true"
            if any(token in normalized for token in ("分别发送", "分别发", "发给他们", "发送给他们", "通知他们", "分别通知"))
            else "false",
        }

    def _build_group_schedule_plan(self, query: str) -> Optional[Dict[str, Any]]:
        parsed = self._parse_group_schedule_request(query)
        if not parsed:
            return None

        group = parsed["group"]
        duration_minutes = parsed["duration_minutes"]
        if parsed.get("date"):
            start, end = self._workday_iso_bounds(date.fromisoformat(parsed["date"]))
            window_label = parsed["date"]
        else:
            start, end = self._next_week_work_bounds()
            window_label = "下周"
        return {
            "summary": f"查看群【{group}】成员{window_label}忙闲，找一个 {duration_minutes} 分钟的共同可用时间并创建会议",
            "relevant_skills": ["lark-im", "lark-calendar", "lark-shared"],
            "references": [
                "lark-im-chat-search.md",
                "lark-calendar-suggestion.md",
                "lark-calendar-create.md",
                "lark-im-messages-send.md",
            ],
            "need_confirmation": True,
            "reason_for_confirmation": "该请求会查询群参与人的忙闲、创建带飞书视频会议的日程并向群发送会议号，属于写操作。",
            "commands": [
                {
                    "command": f"lark-cli im +chat-search --query {self._quote_cli_arg(group)} --format json",
                    "reason": "先按群名搜索 chat_id，后续用该群作为日历参与人查询共同空闲时间。",
                    "expected": "search",
                }
            ],
            "final_response_hint": "找到推荐时间后创建带飞书视频会议的日程，并把会议号和详情发送到群里。",
            "suggestion_window": {"start": start, "end": end},
        }

    def _build_people_schedule_plan(self, query: str) -> Optional[Dict[str, Any]]:
        parsed = self._parse_people_schedule_request(query)
        if not parsed:
            return None

        attendees = json.loads(parsed["attendees"])
        duration_minutes = parsed["duration_minutes"]
        if parsed.get("date"):
            start, end = self._workday_iso_bounds(date.fromisoformat(parsed["date"]))
            window_label = parsed["date"]
        else:
            start, end = self._next_week_work_bounds()
            window_label = "下周"
        return {
            "summary": f"查看{', '.join(attendees)}{window_label}忙闲，找一个 {duration_minutes} 分钟的共同可用时间并创建会议",
            "relevant_skills": ["lark-contact", "lark-calendar", "lark-im", "lark-shared"],
            "references": [
                "lark-contact-search-user.md",
                "lark-calendar-suggestion.md",
                "lark-calendar-create.md",
                "lark-im-messages-send.md",
            ],
            "need_confirmation": True,
            "reason_for_confirmation": "该请求会查询参会人忙闲、创建带飞书视频会议的日程并发送会议信息，属于写操作。",
            "commands": [
                {
                    "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(attendees[0])}",
                    "reason": f"先搜索参会人 {attendees[0]}，获取 open_id。",
                    "expected": "search",
                }
            ],
            "final_response_hint": "找到推荐时间后创建带飞书视频会议的日程，并逐个发送会议信息。",
            "suggestion_window": {"start": start, "end": end},
        }

    def _build_im_send_user_plan(self, query: str) -> Optional[Dict[str, Any]]:
        parsed = self._parse_direct_message_request(query)
        if not parsed:
            return None

        target, message = parsed

        return {
            "summary": f"给飞书用户 {target} 发送消息：{message}",
            "relevant_skills": ["lark-contact", "lark-im", "lark-shared"],
            "references": ["lark-contact-search-user.md", "lark-im-messages-send.md"],
            "need_confirmation": True,
            "reason_for_confirmation": "该请求会发送飞书消息，属于写操作。",
            "commands": [
                {
                    "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(target)}",
                    "reason": "先搜索联系人并获取 open_id。",
                    "expected": "search",
                }
            ],
            "final_response_hint": "找到联系人后继续发送文本消息，并总结发送结果。",
        }

    def _build_im_send_multi_user_plan(self, query: str) -> Optional[Dict[str, Any]]:
        parsed = self._parse_multi_direct_message_request(query)
        if not parsed:
            return None

        targets, message = parsed
        first_target = targets[0]
        return {
            "summary": f"给飞书用户 {', '.join(targets)} 发送消息：{message}",
            "relevant_skills": ["lark-contact", "lark-im", "lark-shared"],
            "references": ["lark-contact-search-user.md", "lark-im-messages-send.md"],
            "need_confirmation": True,
            "reason_for_confirmation": "该请求会发送飞书消息，属于写操作。",
            "commands": [
                {
                    "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(first_target)}",
                    "reason": f"先搜索联系人 {first_target} 并获取 open_id。",
                    "expected": "search",
                }
            ],
            "final_response_hint": "逐个找到联系人并发送私聊消息，最后汇总发送结果。",
        }

    @staticmethod
    def _parse_group_message_request(query: str) -> Optional[Tuple[str, str]]:
        normalized = LarkCLISkill._sanitize_query_text(query)
        patterns = (
            r"(?:给|在)【(?P<group>[^】]+)】(?:里)?发(?:个)?(?:飞书)?消息[：:]\s*(?P<message>.+)$",
            r"在[“\"'](?P<group>[^”\"']+)[”\"']里发(?:个)?[：:]\s*(?P<message>.+)$",
            r"在[“\"'](?P<group>[^”\"']+)[”\"']里发(?:个)?(?:飞书)?消息[：:]\s*(?P<message>.+)$",
            r"给[“\"'](?P<group>[^”\"']+)[”\"']发(?:个)?(?:飞书)?消息[：:]\s*(?P<message>.+)$",
            r"在已有群[:：]?(?:群名叫[:：]?)?[“\"'](?P<group>[^”\"']+)[”\"']里发(?:个)?(?:飞书)?消息?[：:]\s*(?P<message>.+)$",
        )
        match = None
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if match:
                break
        if not match:
            return None

        group = match.group("group").strip()
        message = match.group("message").strip()
        if not group or not message:
            return None
        return group, message

    def _build_im_send_group_plan(self, query: str) -> Optional[Dict[str, Any]]:
        parsed = self._parse_group_message_request(query)
        if not parsed:
            return None

        group, message = parsed

        return {
            "summary": f"给群【{group}】发送消息：{message}",
            "relevant_skills": ["lark-im", "lark-shared"],
            "references": ["lark-im-chat-search.md", "lark-im-messages-send.md"],
            "need_confirmation": True,
            "reason_for_confirmation": "该请求会向群聊发送飞书消息，属于写操作。",
            "commands": [
                {
                    "command": f"lark-cli im +chat-search --query {self._quote_cli_arg(group)} --format json",
                    "reason": "先按群名搜索 chat_id。",
                    "expected": "search",
                }
            ],
            "final_response_hint": "找到群 chat_id 后继续发送文本消息，并总结发送结果。",
        }

    @staticmethod
    def _parse_group_create_request(query: str) -> Optional[Tuple[str, List[str]]]:
        normalized = LarkCLISkill._sanitize_query_text(query)
        if "群" not in normalized:
            return None
        if "发送消息" in normalized or "发消息" in normalized or "发个" in normalized:
            return None
        if not any(
            keyword in normalized
            for keyword in ("拉个群", "拉一个群", "建个群", "建一个群", "建群", "建群聊", "创建群", "创建群聊", "飞书群聊")
        ):
            return None

        title_match = re.search(
            r"(?:名字叫|群名叫|叫做|名称为|名为)\s*([^\n，,。；;]+)",
            normalized,
        )
        if not title_match:
            return None
        group_name = title_match.group(1).strip(" '\"")
        if not group_name:
            return None

        people_text = ""
        formal_match = re.search(r"成员包括(?P<members>.+?)(?:$|，|,|。)", normalized)
        if formal_match:
            people_text = formal_match.group("members").strip()
        else:
            prefix = normalized[: title_match.start()]
            prefix = re.sub(r"^(?:帮我|请|麻烦你)?", "", prefix).strip()
            prefix = re.sub(r"(?:拉个群|拉一个群|拉群|建个群|建一个群|建群|建群聊|创建群|创建群聊|创建一个飞书群聊).*$", "", prefix).strip(" ，,。；;")
            people_text = prefix
        if not people_text:
            return None

        people_text = re.sub(r"^把", "", people_text).strip()
        people_text = people_text.replace("当前用户", "我").replace("以及", "、").replace("和", "、").replace(",", "、").replace("，", "、")
        members = [item.strip() for item in people_text.split("、") if item.strip() and item.strip() not in {"我", "我自己"}]
        deduped: List[str] = []
        for member in members:
            if member not in deduped:
                deduped.append(member)
        if not deduped:
            return None
        return group_name, deduped

    @staticmethod
    def _tomorrow_three_pm_range() -> Tuple[str, str]:
        tz = datetime.now().astimezone().tzinfo
        tomorrow = date.today() + timedelta(days=1)
        start_dt = datetime.combine(tomorrow, time(hour=15, minute=0), tzinfo=tz)
        end_dt = start_dt + timedelta(hours=1)
        return start_dt.isoformat(timespec="minutes"), end_dt.isoformat(timespec="minutes")

    @staticmethod
    def _parse_chinese_number(text: str) -> Optional[int]:
        value = (text or "").strip()
        if not value:
            return None
        if value.isdigit():
            return int(value)
        if value in CN_NUMBER_MAP:
            return CN_NUMBER_MAP[value]
        if value == "十一":
            return 11
        if value == "十二":
            return 12
        if value.startswith("十") and len(value) == 2 and value[1] in CN_NUMBER_MAP:
            return 10 + CN_NUMBER_MAP[value[1]]
        if value.endswith("十") and len(value) == 2 and value[0] in CN_NUMBER_MAP:
            return CN_NUMBER_MAP[value[0]] * 10
        if "十" in value and len(value) == 3:
            left, _, right = value.partition("十")
            if left in CN_NUMBER_MAP and right in CN_NUMBER_MAP:
                return CN_NUMBER_MAP[left] * 10 + CN_NUMBER_MAP[right]
        return None

    @classmethod
    def _parse_hour_and_minute(cls, text: str) -> Optional[Tuple[int, int]]:
        normalized = text or ""

        colon_match = re.search(r"(?P<hour>\d{1,2})\s*[:：]\s*(?P<minute>\d{1,2})", normalized)
        if colon_match:
            hour = int(colon_match.group("hour"))
            minute = int(colon_match.group("minute"))
        else:
            match = re.search(
                r"(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*点(?:(?P<half>半)|(?P<minute>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*分?)?",
                normalized,
            )
            if not match:
                return None
            hour = cls._parse_chinese_number(match.group("hour") or "")
            if hour is None:
                return None
            if match.group("half"):
                minute = 30
            elif match.group("minute"):
                minute_value = cls._parse_chinese_number(match.group("minute") or "")
                minute = minute_value if minute_value is not None else 0
            else:
                minute = 0

        meridiem = ""
        for token in ("凌晨", "早上", "上午", "中午", "下午", "晚上", "晚上", "傍晚"):
            if token in normalized:
                meridiem = token
                break

        if meridiem in {"下午", "晚上", "傍晚"} and 1 <= hour < 12:
            hour += 12
        elif meridiem == "中午" and hour < 11:
            hour += 12
        elif meridiem in {"凌晨"} and hour == 12:
            hour = 0

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    @staticmethod
    def _parse_target_date(text: str) -> Optional[date]:
        normalized = text or ""
        explicit_match = re.search(
            r"(?P<year>20\d{2})\s*[-/年\.]\s*(?P<month>\d{1,2})\s*[-/月\.]\s*(?P<day>\d{1,2})\s*日?",
            normalized,
        )
        if explicit_match:
            return date(
                int(explicit_match.group("year")),
                int(explicit_match.group("month")),
                int(explicit_match.group("day")),
            )
        if "明天" in normalized:
            return date.today() + timedelta(days=1)
        if "今天" in normalized:
            return date.today()
        return None

    @classmethod
    def _parse_calendar_range(cls, text: str) -> Optional[Tuple[str, str]]:
        target_date = cls._parse_target_date(text)
        target_time = cls._parse_hour_and_minute(text)
        if not target_date or not target_time:
            return None

        tz = datetime.now().astimezone().tzinfo
        hour, minute = target_time
        start_dt = datetime.combine(target_date, time(hour=hour, minute=minute), tzinfo=tz)
        end_dt = start_dt + timedelta(hours=1)
        return start_dt.isoformat(timespec="seconds"), end_dt.isoformat(timespec="seconds")

    @staticmethod
    def _extract_calendar_summary(query: str) -> str:
        normalized = LarkCLISkill._sanitize_query_text(query)
        summary_match = re.search(
            r"(?:主题|标题|名称|名字)[：:]\s*(?P<title>[^，,。；;]+)",
            normalized,
        )
        if summary_match:
            summary = summary_match.group("title").strip()
            if summary:
                return summary

        if "评审" in normalized:
            return "需求评审会"
        if "开会" in normalized or "会议" in normalized or "日程" in normalized:
            return "会议"
        return "日程"

    @staticmethod
    def _is_terminal_write_command(command: str) -> bool:
        normalized = (command or "").strip().lower()
        normalized = re.sub(r"^lark-cli\s+base\s+create\b", "lark-cli base +base-create", normalized)
        normalized = re.sub(r"^lark-cli\s+docs\s+create\b", "lark-cli docs +create", normalized)
        normalized = re.sub(r"^lark-cli\s+im\s+messages-send\b", "lark-cli im +messages-send", normalized)
        normalized = re.sub(r"^lark-cli\s+im\s+chat-create\b", "lark-cli im +chat-create", normalized)
        normalized = re.sub(r"^lark-cli\s+calendar\s+create\b", "lark-cli calendar +create", normalized)
        return normalized.startswith(
            (
                "lark-cli base +base-create",
                "lark-cli calendar +create",
                "lark-cli calendar events create",
                "lark-cli calendar event.attendees create",
                "lark-cli im +messages-send",
                "lark-cli im +chat-create",
                "lark-cli docs +create",
            )
        )

    @staticmethod
    def _parse_attendees(query: str) -> List[str]:
        normalized = LarkCLISkill._sanitize_query_text(query)
        match = re.search(r"(?:参与人|参会人|邀请(?:人)?|邀请参会人)[：:：]?\s*(?P<people>.+)$", normalized)
        if match:
            people_text = match.group("people").strip()
        else:
            people_text = ""
            contextual_patterns = (
                r"(?:我和|我跟|我与)(?P<people>.+?)(?:开(?:个|一场|一次)?会|开会议|会议|讨论会|碰一下|沟通)",
                r"(?:和|跟|与)(?P<people>.+?)(?:开(?:个|一场|一次)?会|开会议|会议|讨论会|碰一下|沟通)",
            )
            for pattern in contextual_patterns:
                contextual_match = re.search(pattern, normalized)
                if contextual_match:
                    people_text = contextual_match.group("people").strip()
                    break
            if not people_text:
                return []
        people_text = re.split(
            r"(?:并|，并|,并)?(?:把|将).*(?:发到|发送到|发给|发送给)|(?:并|，并|,并)?(?:通知|同步到)",
            people_text,
            maxsplit=1,
        )[0].strip(" ，,。；;")
        people_text = people_text.replace("和", "、").replace("以及", "、").replace(",", "、").replace("，", "、")
        attendees = [item.strip() for item in people_text.split("、") if item.strip() and item.strip() not in {"我", "我自己"}]
        deduped: List[str] = []
        for attendee in attendees:
            if attendee not in deduped:
                deduped.append(attendee)
        return deduped

    @staticmethod
    def _parse_calendar_link_target_group(query: str) -> Optional[str]:
        normalized = LarkCLISkill._sanitize_query_text(query)
        if not any(token in normalized for token in ("会议", "日程", "会议信息", "会议链接", "日程链接", "会议地址", "链接")):
            return None
        patterns = (
            r"(?:把|将).*(?:发到|发送到|发给|发送给|同步到|通知到|分享到|转发到)\s*【(?P<group>[^】]+)】",
            r"(?:把|将).*(?:发到|发送到|发给|发送给|同步到|通知到|分享到|转发到)\s*(?P<group>[^，,。；;\s]+群)",
            r"(?:发到|发送到|发给|发送给|同步到|通知到|分享到|转发到)\s*【(?P<group>[^】]+)】",
            r"(?:发到|发送到|发给|发送给|同步到|通知到|分享到|转发到)\s*(?P<group>[^，,。；;\s]+群)",
        )
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if match:
                group = match.group("group").strip()
                if group:
                    return group
        return None

    def _has_pending_calendar_group_share(
        self,
        query: str,
        execution_results: List[Dict[str, Any]],
    ) -> bool:
        if not self._parse_calendar_link_target_group(query):
            return False
        event_created = any(
            item.get("success")
            and str(item.get("command", "")).startswith("lark-cli calendar +create ")
            for item in execution_results
        )
        if not event_created:
            return False
        shared_to_group = any(
            item.get("success")
            and str(item.get("command", "")).startswith("lark-cli im +messages-send ")
            and "--chat-id" in str(item.get("command", ""))
            for item in execution_results
        )
        return not shared_to_group

    @staticmethod
    def _has_pending_followup_write(query: str, execution_results: List[Dict[str, Any]]) -> bool:
        normalized = LarkCLISkill._sanitize_query_text(query)
        multi_direct = LarkCLISkill._parse_multi_direct_message_request(normalized)
        if multi_direct:
            targets, _ = multi_direct
            sent_count = sum(
                1
                for item in execution_results
                if item.get("success")
                and str(item.get("command", "")).startswith("lark-cli im +messages-send ")
                and "--user-id" in str(item.get("command", ""))
            )
            return sent_count < len(targets)

        attendees = LarkCLISkill._parse_attendees(normalized)
        asks_to_notify_each_attendee = bool(attendees) and any(
            token in normalized
            for token in ("分别发送", "分别发", "发给他们", "发送给他们", "通知他们", "分别通知")
        )
        if asks_to_notify_each_attendee:
            calendar_created = any(
                item.get("success")
                and (
                    str(item.get("command", "")).startswith("lark-cli calendar +create ")
                    or str(item.get("command", "")).startswith("lark-cli calendar events create ")
                )
                for item in execution_results
            )
            sent_count = sum(
                1
                for item in execution_results
                if item.get("success")
                and str(item.get("command", "")).startswith("lark-cli im +messages-send ")
                and "--user-id" in str(item.get("command", ""))
            )
            return calendar_created and sent_count < len(attendees)

        asks_to_send = any(token in normalized for token in ("发到", "发在", "发送到", "发给", "发送给", "通知", "同步到", "分享给", "转发到"))
        has_explicit_message_target = any(token in normalized for token in ("群", "消息", "链接", "通知", "同步", "分享", "转发", "卡片"))
        if "参与人" in normalized or "参会人" in normalized:
            has_explicit_message_target = has_explicit_message_target and any(
                token in normalized for token in ("群", "消息", "链接", "通知", "同步", "分享", "转发")
            )
        if not asks_to_send:
            return False
        if not has_explicit_message_target:
            return False
        has_non_im_write = any(
            item.get("success")
            and LarkCLISkill._is_terminal_write_command(str(item.get("command", "")))
            and not str(item.get("command", "")).startswith("lark-cli im +messages-send ")
            for item in execution_results
        )
        has_im_send = any(
            item.get("success")
            and str(item.get("command", "")).startswith("lark-cli im +messages-send ")
            for item in execution_results
        )
        return has_non_im_write and not has_im_send

    @staticmethod
    def _execution_result_counts_as_success(item: Dict[str, Any]) -> bool:
        return bool(item.get("success") or item.get("superseded_by_repair"))

    def _build_im_group_search_plan(self, query: str) -> Optional[Dict[str, Any]]:
        match = re.search(
            r"在【(?P<group>[^】]+)】里搜索关键词[：:]\s*(?P<keyword>.+)$",
            self._sanitize_query_text(query),
        )
        if not match:
            return None

        group = match.group("group").strip()
        keyword = match.group("keyword").strip()
        if not group or not keyword:
            return None

        return {
            "summary": f"在群【{group}】中搜索关键词：{keyword}",
            "relevant_skills": ["lark-im", "lark-shared"],
            "references": ["lark-im-chat-search.md", "lark-im-messages-search.md"],
            "need_confirmation": False,
            "reason_for_confirmation": "",
            "commands": [
                {
                    "command": f"lark-cli im +chat-search --query {self._quote_cli_arg(group)} --format json",
                    "reason": "先按群名搜索 chat_id。",
                    "expected": "search",
                }
            ],
            "final_response_hint": "找到群 chat_id 后继续执行消息搜索，并整理匹配结果。",
        }

    def _build_im_recent_summary_plan(self, query: str) -> Optional[Dict[str, Any]]:
        match = re.search(
            r"把(?:【(?P<group>[^】]+)】里?)?最近\s*(?P<count>\d+)\s*条群消息整理成摘要",
            self._sanitize_query_text(query),
        )
        if not match:
            return None

        count = int(match.group("count"))
        group = (match.group("group") or "").strip()
        if count <= 0:
            return None

        if group:
            command = f"lark-cli im +chat-search --query {self._quote_cli_arg(group)} --format json"
            refs = ["lark-im-chat-search.md", "lark-im-chat-messages-list.md"]
            summary = f"读取群【{group}】最近 {count} 条消息并整理摘要"
        else:
            command = "lark-cli im +chat-search --query \"群\" --page-size 10 --format json"
            refs = ["lark-im-chat-search.md", "lark-im-chat-messages-list.md"]
            summary = f"读取最近群聊消息并整理最近 {count} 条摘要"

        return {
            "summary": summary,
            "relevant_skills": ["lark-im", "lark-shared"],
            "references": refs,
            "need_confirmation": False,
            "reason_for_confirmation": "",
            "commands": [{"command": command, "reason": "先定位目标群。", "expected": "search"}],
            "final_response_hint": "读取最近消息后，提炼成简短摘要。",
        }

    def _build_calendar_agenda_plan(self, query: str) -> Optional[Dict[str, Any]]:
        normalized = self._sanitize_query_text(query)
        if "日程" not in normalized:
            return None

        if "今天" in normalized:
            start, end = self._today_range()
            summary = "查看今天的日程"
        elif "这周" in normalized or "本周" in normalized:
            start, end = self._this_week_range()
            summary = "查看本周的日程"
        else:
            return None

        command = f"lark-cli calendar +agenda --start {self._quote_cli_arg(start)} --end {self._quote_cli_arg(end)}"
        return {
            "summary": summary,
            "relevant_skills": ["lark-calendar", "lark-shared"],
            "references": ["lark-calendar-agenda.md"],
            "need_confirmation": False,
            "reason_for_confirmation": "",
            "commands": [{"command": command, "reason": "按时间范围读取日程。", "expected": "read"}],
            "final_response_hint": "把日程按时间顺序整理展示。",
        }

    def _build_calendar_create_plan(self, query: str) -> Optional[Dict[str, Any]]:
        normalized = self._sanitize_query_text(query)
        if not any(keyword in normalized for keyword in ("日程", "会议")):
            return None
        if not any(keyword in normalized for keyword in ("添加", "创建", "安排", "拉个", "拉一个", "拉")):
            return None
        parsed_range = self._parse_calendar_range(normalized)
        if not parsed_range and "明天下午三点" not in normalized and "明天 15" not in normalized:
            return None

        summary = self._extract_calendar_summary(normalized)
        start, end = parsed_range or self._tomorrow_three_pm_range()
        attendees = self._parse_attendees(normalized)
        if attendees:
            first_attendee = attendees[0]
            relevant_skills = ["lark-contact", "lark-calendar", "lark-shared"]
            references = ["lark-contact-search-user.md", "lark-calendar-create.md"]
            if self._parse_calendar_link_target_group(normalized):
                relevant_skills.extend(["lark-im", "lark-vc"])
                references.extend(["lark-im-chat-search.md", "lark-im-messages-send.md"])
            relevant_skills = list(dict.fromkeys(relevant_skills))
            references = list(dict.fromkeys(references))
            return {
                "summary": f"创建会议日程：{summary}，参与人：{', '.join(attendees)}",
                "relevant_skills": relevant_skills,
                "references": references,
                "need_confirmation": True,
                "reason_for_confirmation": "该请求会创建飞书日程并邀请参与人，属于写操作。",
                "commands": [
                    {
                        "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(first_attendee)}",
                        "reason": "先搜索第一个参会人并获取 open_id。",
                        "expected": "search",
                    }
                ],
                "final_response_hint": "收集完参会人 open_id 后创建会议日程。",
            }
        command = (
            f"lark-cli calendar +create --summary {self._quote_cli_arg(summary)} "
            f"--start {self._quote_cli_arg(start)} --end {self._quote_cli_arg(end)} --as user"
        )
        return {
            "summary": f"创建会议日程：{summary}",
            "relevant_skills": ["lark-calendar", "lark-shared"],
            "references": ["lark-calendar-create.md"],
            "need_confirmation": True,
            "reason_for_confirmation": "该请求会创建飞书日程，属于写操作。",
            "commands": [{"command": command, "reason": "按标准时间格式创建会议日程。", "expected": "write"}],
            "final_response_hint": "总结日程是否创建成功，并返回 event_id 等关键结果。",
        }

    def _build_contact_search_plan(self, query: str) -> Optional[Dict[str, Any]]:
        normalized = self._sanitize_query_text(query)
        match = re.search(r"(?:搜索同事|搜索联系人|查找同事|查找联系人)[：:\s]*(?P<name>.+)$", normalized)
        if not match:
            return None

        name = match.group("name").strip()
        if not name:
            return None

        return {
            "summary": f"搜索同事：{name}",
            "relevant_skills": ["lark-contact", "lark-shared"],
            "references": ["lark-contact-search-user.md"],
            "need_confirmation": False,
            "reason_for_confirmation": "",
            "commands": [
                {
                    "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(name)}",
                    "reason": "按姓名/邮箱/手机号搜索联系人。",
                    "expected": "search",
                }
            ],
            "final_response_hint": "返回最匹配的联系人信息。",
        }

    def _build_task_query_plan(self, query: str) -> Optional[Dict[str, Any]]:
        normalized = self._sanitize_query_text(query)
        if "待办任务" in normalized and "今天" in normalized:
            command = "lark-cli task +get-my-tasks --complete=false"
            summary = "查看我今天的待办任务"
        elif "已完成" in normalized and "任务" in normalized:
            command = "lark-cli task +get-my-tasks --complete=true"
            summary = "查看我已完成的任务"
        else:
            return None

        return {
            "summary": summary,
            "relevant_skills": ["lark-task", "lark-shared"],
            "references": ["lark-task-get-my-tasks.md"],
            "need_confirmation": False,
            "reason_for_confirmation": "",
            "commands": [{"command": command, "reason": "读取当前用户任务列表。", "expected": "read"}],
            "final_response_hint": "整理任务标题、状态和截止时间。",
        }

    def _build_group_create_plan(self, query: str) -> Optional[Dict[str, Any]]:
        parsed = self._parse_group_create_request(query)
        if not parsed:
            return None

        group_name, members = parsed
        first_member = members[0]
        return {
            "summary": f"创建名为《{group_name}》的群聊，并拉入：{', '.join(['我', *members])}",
            "relevant_skills": ["lark-contact", "lark-im", "lark-shared"],
            "references": ["lark-contact-search-user.md", "lark-im-chat-create.md"],
            "need_confirmation": True,
            "reason_for_confirmation": "该请求会创建群聊并邀请成员，属于写操作。",
            "commands": [
                {
                    "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(first_member)}",
                    "reason": "先搜索第一个联系人并获取 open_id。",
                    "expected": "search",
                }
            ],
            "final_response_hint": "拿到全部成员 open_id 后，用 +chat-create 创建群聊并邀请成员。",
        }

    def _build_heuristic_plan(self, query: str) -> Optional[Dict[str, Any]]:
        for builder in (
            self._build_doc_create_plan,
            self._build_bitable_import_plan,
            self._build_base_create_plan,
            self._build_group_schedule_plan,
            self._build_people_schedule_plan,
            self._build_im_send_multi_user_plan,
            self._build_im_send_user_plan,
            self._build_im_send_group_plan,
            self._build_im_group_search_plan,
            self._build_im_recent_summary_plan,
            self._build_group_create_plan,
            self._build_calendar_agenda_plan,
            self._build_calendar_create_plan,
            self._build_contact_search_plan,
            self._build_task_query_plan,
        ):
            plan = builder(query)
            if plan:
                return plan
        return None

    def _build_heuristic_step(
        self,
        query: str,
        execution_results: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        normalized = self._sanitize_query_text(query)
        if execution_results:
            last_result = execution_results[-1]
            group_schedule = self._parse_group_schedule_request(normalized)
            multi_direct_message = self._parse_multi_direct_message_request(normalized)
            if multi_direct_message:
                targets, _ = multi_direct_message
                sent_count = sum(
                    1
                    for item in execution_results
                    if item.get("success")
                    and str(item.get("command", "")).startswith("lark-cli im +messages-send ")
                    and "--user-id" in str(item.get("command", ""))
                )
                if sent_count >= len(targets):
                    return {"done": True, "summary": "所有联系人消息均已发送完成。"}

            people_schedule = self._parse_people_schedule_request(normalized)
            if people_schedule and people_schedule.get("notify_each") == "true":
                attendees = json.loads(people_schedule["attendees"])
                sent_count = sum(
                    1
                    for item in execution_results
                    if item.get("success")
                    and str(item.get("command", "")).startswith("lark-cli im +messages-send ")
                    and "--user-id" in str(item.get("command", ""))
                )
                if sent_count >= len(attendees):
                    return {"done": True, "summary": "会议日程已创建，会议信息也已分别发送给所有参会人。"}

            last_command = str(last_result.get("command", ""))
            last_message_missing_meeting_details = (
                bool(group_schedule)
                and any(
                    token in normalized
                    for token in ("会议号", "会议链接", "会议地址", "加入会议", "详情", "视频会议")
                )
                and last_result.get("success")
                and last_command.startswith("lark-cli im +messages-send ")
                and "--chat-id" in last_command
                and "会议号" not in last_command
                and "会议链接" not in last_command
                and "vc.feishu.cn" not in last_command
                and "meetings.feishu.cn" not in last_command
            )
            if (
                last_result.get("success")
                and self._is_terminal_write_command(last_result.get("command", ""))
                and not last_message_missing_meeting_details
                and not self._has_pending_calendar_group_share(normalized, execution_results)
                and not self._has_pending_followup_write(normalized, execution_results)
            ):
                return {
                    "done": True,
                    "summary": "核心写操作已成功完成，无需继续追加验证步骤。",
                }

        group_schedule = self._parse_group_schedule_request(normalized)
        if group_schedule:
            group_name = group_schedule["group"]
            summary = group_schedule["summary"]
            duration_minutes = int(group_schedule["duration_minutes"])
            window_label = group_schedule.get("date") or "下周"
            requires_meeting_details = any(
                token in normalized
                for token in ("会议号", "会议链接", "会议地址", "加入会议", "详情", "视频会议")
            )
            chat_id = None
            suggestion_result: Optional[Dict[str, Any]] = None
            calendar_event: Optional[Dict[str, Any]] = None
            attendees_added = False
            sent_card = False
            failed_interactive_card = False

            for item in execution_results:
                command_text = str(item.get("command", ""))
                output_text = item.get("stdout") or item.get("stderr") or ""
                if item.get("success") and command_text.startswith("lark-cli im +chat-search "):
                    chat_id = self._extract_chat_id_from_output(output_text) or chat_id
                if item.get("success") and command_text.startswith("lark-cli calendar +suggestion "):
                    suggestion_result = item
                if item.get("success") and command_text.startswith("lark-cli calendar events create "):
                    calendar_event = item
                elif (
                    item.get("success")
                    and command_text.startswith("lark-cli calendar +create ")
                    and not requires_meeting_details
                ):
                    calendar_event = item
                if item.get("success") and command_text.startswith("lark-cli calendar event.attendees create "):
                    attendees_added = True
                if (
                    item.get("success")
                    and command_text.startswith("lark-cli im +messages-send ")
                    and "--chat-id" in command_text
                    and (
                        not requires_meeting_details
                        or "会议号" in command_text
                        or "会议链接" in command_text
                        or "vc.feishu.cn" in command_text
                        or "meetings.feishu.cn" in command_text
                    )
                ):
                    sent_card = True
                if (
                    not item.get("success")
                    and command_text.startswith("lark-cli im +messages-send ")
                    and "--content" in command_text
                    and "--chat-id" in command_text
                ):
                    failed_interactive_card = True

            if sent_card:
                return {"done": True, "summary": "已找到合适时间、创建带会议号的会议，并把会议号和详情发送到群里。"}

            if not chat_id:
                return {
                    "done": False,
                    "summary": f"查看群【{group_name}】成员{window_label}忙闲，找一个 {duration_minutes} 分钟的共同可用时间并创建会议",
                    "command": f"lark-cli im +chat-search --query {self._quote_cli_arg(group_name)} --format json",
                    "reason": "先按群名搜索 chat_id；日历 suggestion 支持直接用群 ID 作为参与人计算共同可用时间，避免逐个猜测群成员接口参数。",
                    "expected": "search",
                    "final_response_hint": "找到群 chat_id 后查询共同可用时间。",
                    "reason_for_confirmation": "该请求会创建会议并向群发送会议卡片，属于写操作。",
                }

            if not suggestion_result:
                if group_schedule.get("date"):
                    start, end = self._workday_iso_bounds(date.fromisoformat(group_schedule["date"]))
                else:
                    start, end = self._next_week_work_bounds()
                return {
                    "done": False,
                    "summary": f"查看群【{group_name}】成员{window_label}忙闲，找一个 {duration_minutes} 分钟的共同可用时间并创建会议",
                    "command": (
                        f"lark-cli calendar +suggestion --start {self._quote_cli_arg(start)} "
                        f"--end {self._quote_cli_arg(end)} --attendee-ids {self._quote_cli_arg(chat_id)} "
                        f"--duration-minutes {duration_minutes} --timezone Asia/Shanghai --format json"
                    ),
                    "reason": "使用 calendar +suggestion 在目标时间范围内，为整个群查找共同可用的一小时会议时间。",
                    "expected": "read",
                    "final_response_hint": "从推荐结果中选取第一个共同可用时段并继续创建会议。",
                    "reason_for_confirmation": "",
                }

            suggestion_output = suggestion_result.get("stdout") or suggestion_result.get("stderr") or ""
            suggested_range = self._extract_suggestion_range(suggestion_output)
            if not suggested_range:
                return {"done": True, "summary": "已查询下周推荐时间，但未能从 CLI 返回中解析出可直接创建会议的起止时间。"}
            start, end = suggested_range

            if not calendar_event:
                event_data = {
                    "summary": summary,
                    "description": f"根据【{group_name}】成员日历自动选择的共同空闲时段。",
                    "start_time": {
                        "timestamp": self._iso_to_unix_seconds(start),
                        "timezone": "Asia/Shanghai",
                    },
                    "end_time": {
                        "timestamp": self._iso_to_unix_seconds(end),
                        "timezone": "Asia/Shanghai",
                    },
                    "vchat": {
                        "vc_type": "vc",
                        "meeting_settings": {
                            "allow_attendees_start": True,
                            "join_meeting_permission": "only_event_attendees",
                            "open_lobby": False,
                        },
                    },
                    "need_notification": True,
                }
                params = {"calendar_id": "primary", "user_id_type": "open_id"}
                return {
                    "done": False,
                    "summary": f"在群【{group_name}】共同可用时间创建 {duration_minutes} 分钟讨论会",
                    "command": (
                        "lark-cli calendar events create "
                        f"--params {self._quote_cli_arg(json.dumps(params, ensure_ascii=False, separators=(',', ':')))} "
                        f"--data {self._quote_cli_arg(json.dumps(event_data, ensure_ascii=False, separators=(',', ':')))} "
                        "--format json --as user"
                    ),
                    "reason": "已拿到推荐的共同可用时段，显式创建带飞书视频会议的日程，以便获得 meeting_url 和会议号。",
                    "expected": "write",
                    "final_response_hint": "会议创建成功后继续邀请群成员，并把会议号和详情发送到群里。",
                    "reason_for_confirmation": "该请求会创建飞书日程，属于写操作。",
                }

            event_output = calendar_event.get("stdout") or calendar_event.get("stderr") or ""
            meeting_url = self._extract_meeting_url_from_output(event_output)
            meeting_number = self._extract_meeting_number_from_output(event_output)
            event_url = self._extract_url_from_output(event_output)
            event_id = self._extract_event_id_from_output(event_output) or ""
            if not attendees_added and event_id:
                attendee_data = {
                    "attendees": [{"type": "chat", "chat_id": chat_id}],
                    "need_notification": True,
                }
                params = {
                    "calendar_id": "primary",
                    "event_id": event_id,
                    "user_id_type": "open_id",
                }
                return {
                    "done": False,
                    "summary": f"把群【{group_name}】加入会议日程参与人",
                    "command": (
                        "lark-cli calendar event.attendees create "
                        f"--params {self._quote_cli_arg(json.dumps(params, ensure_ascii=False, separators=(',', ':')))} "
                        f"--data {self._quote_cli_arg(json.dumps(attendee_data, ensure_ascii=False, separators=(',', ':')))} "
                        "--format json --as user"
                    ),
                    "reason": "带飞书视频会议的日程已创建，继续把目标群作为日程参与人加入，保证群成员收到日程邀请。",
                    "expected": "write",
                    "final_response_hint": "群成员加入日程后，把会议号和详情发送到群里。",
                    "reason_for_confirmation": "该请求会更新飞书日程参与人，属于写操作。",
                }
            if failed_interactive_card:
                fallback_text = self._build_meeting_plain_text(
                    title=summary,
                    group=f"【{group_name}】",
                    start=start,
                    end=end,
                    event_id=event_id,
                    event_url=event_url,
                    meeting_url=meeting_url,
                    meeting_number=meeting_number,
                )
                return {
                    "done": False,
                    "summary": f"把会议通知补发到群【{group_name}】",
                    "command": (
                        f"lark-cli im +messages-send --chat-id {self._quote_cli_arg(chat_id)} "
                        f"--text {self._quote_lark_text_arg(fallback_text)} --as user"
                    ),
                    "reason": "互动卡片发送失败，自动降级为纯文本会议通知，确保群内收到会议信息且不会出现 Markdown 未解析。",
                    "expected": "write",
                    "final_response_hint": "总结共同时间查询、会议创建和群通知发送是否都成功。",
                    "reason_for_confirmation": "该请求会向飞书群发送会议通知，属于写操作。",
                }
            plain_text = self._build_meeting_plain_text(
                title=summary,
                group=f"【{group_name}】",
                start=start,
                end=end,
                event_id=event_id,
                event_url=event_url,
                meeting_url=meeting_url,
                meeting_number=meeting_number,
            )
            return {
                "done": False,
                "summary": f"把会议号和详情发送到群【{group_name}】",
                "command": (
                    f"lark-cli im +messages-send --chat-id {self._quote_cli_arg(chat_id)} "
                    f"--text {self._quote_lark_text_arg(plain_text)} --as user"
                ),
                "reason": "会议已创建且已获得 VC 会议链接，使用纯文本发送会议号和详情，避免 Markdown 或互动卡片在飞书端未正确解析。",
                "expected": "write",
                "final_response_hint": "总结共同时间查询、会议创建和群卡片发送是否都成功。",
                "reason_for_confirmation": "该请求会向飞书群发送会议卡片，属于写操作。",
            }

        people_schedule = self._parse_people_schedule_request(normalized)
        if people_schedule:
            attendees = json.loads(people_schedule["attendees"])
            summary = people_schedule["summary"]
            duration_minutes = int(people_schedule["duration_minutes"])
            window_label = people_schedule.get("date") or "下周"
            notify_each = people_schedule.get("notify_each") == "true"

            resolved_ids: List[str] = []
            suggestion_result: Optional[Dict[str, Any]] = None
            calendar_event: Optional[Dict[str, Any]] = None
            attendees_added = False
            sent_user_count = 0
            for item in execution_results:
                command_text = str(item.get("command", ""))
                output_text = item.get("stdout") or item.get("stderr") or ""
                resolved = self._extract_open_id_from_output(output_text)
                if item.get("success") and command_text.startswith("lark-cli contact +search-user ") and resolved and resolved not in resolved_ids:
                    resolved_ids.append(resolved)
                if item.get("success") and command_text.startswith("lark-cli calendar +suggestion "):
                    suggestion_result = item
                if item.get("success") and command_text.startswith("lark-cli calendar events create "):
                    calendar_event = item
                if item.get("success") and command_text.startswith("lark-cli calendar event.attendees create "):
                    attendees_added = True
                if item.get("success") and command_text.startswith("lark-cli im +messages-send ") and "--user-id" in command_text:
                    sent_user_count += 1

            if notify_each and sent_user_count >= len(attendees):
                return {"done": True, "summary": "会议日程已创建，会议信息也已分别发送给所有参会人。"}
            if not notify_each and attendees_added:
                return {"done": True, "summary": "会议日程已创建，并已邀请所有参会人。"}

            if len(resolved_ids) < len(attendees):
                next_attendee = attendees[len(resolved_ids)]
                return {
                    "done": False,
                    "summary": f"查看{', '.join(attendees)}{window_label}忙闲，找一个 {duration_minutes} 分钟的共同可用时间并创建会议",
                    "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(next_attendee)}",
                    "reason": f"继续搜索参会人 {next_attendee}，获取 open_id。",
                    "expected": "search",
                    "final_response_hint": "收集完参会人 open_id 后查询共同可用时间。",
                    "reason_for_confirmation": "该请求会创建飞书日程并发送会议信息，属于写操作。",
                }

            if not suggestion_result:
                if people_schedule.get("date"):
                    start, end = self._workday_iso_bounds(date.fromisoformat(people_schedule["date"]))
                else:
                    start, end = self._next_week_work_bounds()
                attendee_ids = ",".join(resolved_ids[: len(attendees)])
                return {
                    "done": False,
                    "summary": f"查看{', '.join(attendees)}{window_label}忙闲，找一个 {duration_minutes} 分钟的共同可用时间并创建会议",
                    "command": (
                        f"lark-cli calendar +suggestion --start {self._quote_cli_arg(start)} "
                        f"--end {self._quote_cli_arg(end)} --attendee-ids {self._quote_cli_arg(attendee_ids)} "
                        f"--duration-minutes {duration_minutes} --timezone Asia/Shanghai --format json"
                    ),
                    "reason": "使用 calendar +suggestion 为所有参会人查找共同可用的一小时会议时间。",
                    "expected": "read",
                    "final_response_hint": "从推荐结果中选取第一个共同可用时段并继续创建会议。",
                    "reason_for_confirmation": "",
                }

            suggestion_output = suggestion_result.get("stdout") or suggestion_result.get("stderr") or ""
            suggested_range = self._extract_suggestion_range(suggestion_output)
            if not suggested_range:
                return {"done": True, "summary": "已查询推荐时间，但未能从 CLI 返回中解析出可直接创建会议的起止时间。"}
            start, end = suggested_range

            if not calendar_event:
                event_data = {
                    "summary": summary,
                    "description": f"根据{', '.join(attendees)}的日历自动选择的共同空闲时段。",
                    "start_time": {
                        "timestamp": self._iso_to_unix_seconds(start),
                        "timezone": "Asia/Shanghai",
                    },
                    "end_time": {
                        "timestamp": self._iso_to_unix_seconds(end),
                        "timezone": "Asia/Shanghai",
                    },
                    "vchat": {
                        "vc_type": "vc",
                        "meeting_settings": {
                            "allow_attendees_start": True,
                            "join_meeting_permission": "only_event_attendees",
                            "open_lobby": False,
                        },
                    },
                    "need_notification": True,
                }
                params = {"calendar_id": "primary", "user_id_type": "open_id"}
                return {
                    "done": False,
                    "summary": f"在共同可用时间创建 {duration_minutes} 分钟会议",
                    "command": (
                        "lark-cli calendar events create "
                        f"--params {self._quote_cli_arg(json.dumps(params, ensure_ascii=False, separators=(',', ':')))} "
                        f"--data {self._quote_cli_arg(json.dumps(event_data, ensure_ascii=False, separators=(',', ':')))} "
                        "--format json --as user"
                    ),
                    "reason": "已拿到推荐的共同可用时段，显式创建带飞书视频会议的日程，以便获得 meeting_url 和会议号。",
                    "expected": "write",
                    "final_response_hint": "会议创建成功后继续邀请参会人，并按需分别发送会议信息。",
                    "reason_for_confirmation": "该请求会创建飞书日程，属于写操作。",
                }

            event_output = calendar_event.get("stdout") or calendar_event.get("stderr") or ""
            meeting_url = self._extract_meeting_url_from_output(event_output)
            meeting_number = self._extract_meeting_number_from_output(event_output)
            event_url = self._extract_url_from_output(event_output)
            event_id = self._extract_event_id_from_output(event_output) or ""

            if not attendees_added and event_id:
                attendee_data = {
                    "attendees": [{"type": "user", "user_id": open_id} for open_id in resolved_ids[: len(attendees)]],
                    "need_notification": True,
                }
                params = {
                    "calendar_id": "primary",
                    "event_id": event_id,
                    "user_id_type": "open_id",
                }
                return {
                    "done": False,
                    "summary": f"把{', '.join(attendees)}加入会议日程参与人",
                    "command": (
                        "lark-cli calendar event.attendees create "
                        f"--params {self._quote_cli_arg(json.dumps(params, ensure_ascii=False, separators=(',', ':')))} "
                        f"--data {self._quote_cli_arg(json.dumps(attendee_data, ensure_ascii=False, separators=(',', ':')))} "
                        "--format json --as user"
                    ),
                    "reason": "带飞书视频会议的日程已创建，继续把参会人加入日程，保证他们收到日程邀请。",
                    "expected": "write",
                    "final_response_hint": "参会人加入日程后，按需分别发送会议信息。",
                    "reason_for_confirmation": "该请求会更新飞书日程参与人，属于写操作。",
                }

            if notify_each and sent_user_count < len(attendees):
                target_name = attendees[sent_user_count]
                target_open_id = resolved_ids[sent_user_count]
                message = self._build_meeting_plain_text(
                    title=summary,
                    group="、".join(attendees),
                    start=start,
                    end=end,
                    event_id=event_id,
                    event_url=event_url,
                    meeting_url=meeting_url,
                    meeting_number=meeting_number,
                )
                return {
                    "done": False,
                    "summary": f"把会议信息分别发送给{', '.join(attendees)}",
                    "command": f"lark-cli im +messages-send --user-id {self._quote_cli_arg(target_open_id)} --text {self._quote_lark_text_arg(message)} --as user",
                    "reason": f"已创建会议并加入参会人，继续把会议信息发送给 {target_name}。",
                    "expected": "write",
                    "final_response_hint": "继续给剩余参会人发送会议信息，并汇总发送结果。",
                    "reason_for_confirmation": "该请求会向飞书用户发送消息，属于写操作。",
                }

            return {"done": True, "summary": "会议日程已创建，并已邀请所有参会人。"}

        bitable_import = self._parse_bitable_import_request(normalized)
        if bitable_import:
            title = bitable_import["title"]
            file_path = bitable_import["file_path"]
            group = bitable_import.get("group")
            summary = f"将本地文件 {file_path} 导入为多维表格《{title}》"
            if group:
                summary += f"，并发送到群【{group}】"

            import_result: Optional[Dict[str, Any]] = None
            task_result: Optional[Dict[str, Any]] = None
            for item in execution_results:
                command_text = str(item.get("command", ""))
                if item.get("success") and command_text.startswith("lark-cli drive +import "):
                    import_result = item
                if item.get("success") and command_text.startswith("lark-cli drive +task_result "):
                    task_result = item

            if not import_result:
                return {
                    "done": False,
                    "summary": summary,
                    "command": (
                        f"lark-cli drive +import --file {self._quote_cli_arg(file_path)} "
                        f"--type bitable --name {self._quote_cli_arg(title)} --as user"
                    ),
                    "reason": "本地 Excel/CSV 导入为多维表格应使用 drive +import，并指定 type=bitable。",
                    "expected": "write",
                    "final_response_hint": "导入完成后继续获取结果；如果用户要求发群，还要继续发送到群。",
                    "reason_for_confirmation": "该请求会上传本地文件并创建飞书多维表格，属于写操作。",
                }

            import_output = import_result.get("stdout") or import_result.get("stderr") or ""
            next_command = self._extract_next_command_from_output(import_output)
            if next_command and not task_result:
                return {
                    "done": False,
                    "summary": summary,
                    "command": self._repair_command(next_command),
                    "reason": "导入任务返回了异步结果查询命令，继续获取最终多维表格链接或 token。",
                    "expected": "read",
                    "final_response_hint": "拿到导入结果后继续判断是否需要发群。",
                    "reason_for_confirmation": "",
                }

            result_output = ""
            if task_result:
                result_output = task_result.get("stdout") or task_result.get("stderr") or ""
            if not result_output:
                result_output = import_output

            if not group:
                return {"done": True, "summary": "多维表格已导入完成。"}

            chat_id = None
            for item in execution_results:
                command_text = str(item.get("command", ""))
                if item.get("success") and command_text.startswith("lark-cli im +chat-search "):
                    chat_id = self._extract_chat_id_from_output(item.get("stdout") or item.get("stderr") or "")
                    if chat_id:
                        break

            sent_to_group = any(
                item.get("success")
                and str(item.get("command", "")).startswith("lark-cli im +messages-send ")
                and "--chat-id" in str(item.get("command", ""))
                for item in execution_results
            )
            if sent_to_group:
                return {"done": True, "summary": "多维表格已导入完成，并已发送到目标群。"}

            if not chat_id:
                return {
                    "done": False,
                    "summary": summary,
                    "command": f"lark-cli im +chat-search --query {self._quote_cli_arg(group)} --format json",
                    "reason": f"多维表格已导入，继续搜索群【{group}】的 chat_id，以便发送结果。",
                    "expected": "search",
                    "final_response_hint": "找到群 chat_id 后发送多维表格链接或 token。",
                    "reason_for_confirmation": "该请求会向飞书群发送消息，属于写操作。",
                }

            result_url = self._extract_url_from_output(result_output)
            result_token = self._extract_token_from_output(result_output)
            if result_url:
                message = f"多维表格《{title}》已创建并导入完成：{result_url}"
            elif result_token:
                message = f"多维表格《{title}》已创建并导入完成。当前 CLI 未返回可直接打开的链接，token：{result_token}。"
            else:
                message = f"多维表格《{title}》已创建并导入完成。当前 CLI 未返回可直接打开的链接或 token。"
            return {
                "done": False,
                "summary": summary,
                "command": f"lark-cli im +messages-send --chat-id {self._quote_cli_arg(chat_id)} --text {self._quote_lark_text_arg(message)}",
                "reason": "已获取目标群 chat_id，继续发送多维表格链接或导入结果，完成用户要求的后置通知。",
                "expected": "write",
                "final_response_hint": "总结多维表格导入和群通知是否都成功。",
                "reason_for_confirmation": "该请求会向飞书群发送消息，属于写操作。",
            }

        multi_direct_message = self._parse_multi_direct_message_request(normalized)
        if multi_direct_message:
            targets, message = multi_direct_message
            resolved_ids: List[str] = []
            for item in execution_results:
                resolved = self._extract_open_id_from_output(item.get("stdout") or item.get("stderr") or "")
                if resolved and resolved not in resolved_ids:
                    resolved_ids.append(resolved)

            if len(execution_results) < len(targets):
                next_target = targets[len(execution_results)]
                return {
                    "done": False,
                    "summary": f"向用户{', '.join(targets)}发送飞书文本消息：'{message}'",
                    "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(next_target)}",
                    "reason": f"先搜索联系人 {next_target}，获取 open_id。",
                    "expected": "search",
                    "final_response_hint": "收集完所有联系人 open_id 后逐个发送消息。",
                    "reason_for_confirmation": "该请求会发送飞书消息，属于写操作。",
                }

            sent_count = sum(
                1
                for item in execution_results
                if item.get("success") and str(item.get("command", "")).startswith("lark-cli im +messages-send ")
            )
            if sent_count < len(targets) and sent_count < len(resolved_ids):
                open_id = resolved_ids[sent_count]
                current_target = targets[sent_count]
                return {
                    "done": False,
                    "summary": f"向用户{', '.join(targets)}发送飞书文本消息：'{message}'",
                    "command": f"lark-cli im +messages-send --user-id {self._quote_cli_arg(open_id)} --text {self._quote_lark_text_arg(message)}",
                    "reason": f"已获取 {current_target} 的 open_id，继续发送私聊消息。",
                    "expected": "write",
                    "final_response_hint": "继续给剩余联系人发送消息，并汇总发送结果。",
                    "reason_for_confirmation": "该请求会发送飞书消息，属于写操作。",
                }

            if sent_count >= len(targets):
                return {"done": True, "summary": "所有联系人消息均已发送完成。"}

            return {"done": True, "summary": "未能收集齐所有联系人 open_id，停止继续发送。"}

        direct_message = self._parse_direct_message_request(normalized)
        if direct_message:
            target, message = direct_message
            if not execution_results:
                return {
                    "done": False,
                    "summary": f"向用户{target}发送飞书文本消息：'{message}'",
                    "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(target)}",
                    "reason": "先通过通讯录搜索联系人，获取 open_id。",
                    "expected": "search",
                    "final_response_hint": "找到联系人后继续发送消息。",
                    "reason_for_confirmation": "该请求会发送飞书消息，属于写操作。",
                }
            open_id = self._extract_open_id_from_output(
                execution_results[-1].get("stdout") or execution_results[-1].get("stderr") or ""
            )
            if open_id:
                return {
                    "done": False,
                    "summary": f"向用户{target}发送飞书文本消息：'{message}'",
                    "command": f"lark-cli im +messages-send --user-id {self._quote_cli_arg(open_id)} --text {self._quote_lark_text_arg(message)}",
                    "reason": "已获取联系人 open_id，继续发送私聊消息。",
                    "expected": "write",
                    "final_response_hint": "总结消息是否发送成功。",
                    "reason_for_confirmation": "该请求会发送飞书消息，属于写操作。",
                }
            return {"done": True, "summary": "联系人搜索未解析到 open_id，停止继续发送。"}

        group_send = self._parse_group_message_request(normalized)
        if group_send:
            group_name, message = group_send
            if not execution_results:
                return {
                    "done": False,
                    "summary": f"向群【{group_name}】发送飞书文本消息：'{message}'",
                    "command": f"lark-cli im +chat-search --query {self._quote_cli_arg(group_name)} --format json",
                    "reason": "先按群名搜索 chat_id。",
                    "expected": "search",
                    "final_response_hint": "找到群 chat_id 后继续发送消息。",
                    "reason_for_confirmation": "该请求会发送飞书消息，属于写操作。",
                }
            chat_id = self._extract_chat_id_from_output(
                execution_results[-1].get("stdout") or execution_results[-1].get("stderr") or ""
            )
            if chat_id:
                return {
                    "done": False,
                    "summary": f"向群【{group_name}】发送飞书文本消息：'{message}'",
                    "command": f"lark-cli im +messages-send --chat-id {self._quote_cli_arg(chat_id)} --text {self._quote_lark_text_arg(message)}",
                    "reason": "已获取 chat_id，继续发送群消息。",
                    "expected": "write",
                    "final_response_hint": "总结消息是否发送成功。",
                    "reason_for_confirmation": "该请求会发送飞书消息，属于写操作。",
                }
            return {"done": True, "summary": "群搜索未解析到 chat_id，停止继续发送。"}

        group_search = re.search(r"在【(?P<group>[^】]+)】里搜索关键词[：:]\s*(?P<keyword>.+)$", normalized)
        if group_search:
            if not execution_results:
                return {
                    "done": False,
                    "summary": f"在群【{group_search.group('group').strip()}】里搜索关键词：{group_search.group('keyword').strip()}",
                    "command": f"lark-cli im +chat-search --query {self._quote_cli_arg(group_search.group('group').strip())} --format json",
                    "reason": "先按群名搜索 chat_id。",
                    "expected": "search",
                    "final_response_hint": "找到群 chat_id 后执行消息搜索。",
                    "reason_for_confirmation": "",
                }
            chat_id = self._extract_chat_id_from_output(
                execution_results[-1].get("stdout") or execution_results[-1].get("stderr") or ""
            )
            if chat_id:
                return {
                    "done": False,
                    "summary": f"在群【{group_search.group('group').strip()}】里搜索关键词：{group_search.group('keyword').strip()}",
                    "command": f"lark-cli im +messages-search --query {self._quote_cli_arg(group_search.group('keyword').strip())} --chat-id {self._quote_cli_arg(chat_id)} --format json",
                    "reason": "已获取群 chat_id，继续搜索群消息。",
                    "expected": "read",
                    "final_response_hint": "整理匹配到的群消息。",
                    "reason_for_confirmation": "",
                }
            return {"done": True, "summary": "群搜索未解析到 chat_id，停止继续搜索消息。"}

        recent_summary = re.search(
            r"把(?:【(?P<group>[^】]+)】里?)?最近\s*(?P<count>\d+)\s*条群消息整理成摘要",
            normalized,
        )
        if recent_summary:
            count = int(recent_summary.group("count"))
            group = (recent_summary.group("group") or "").strip()
            if not execution_results and group:
                return {
                    "done": False,
                    "summary": f"读取群【{group}】最近 {count} 条消息并整理摘要",
                    "command": f"lark-cli im +chat-search --query {self._quote_cli_arg(group)} --format json",
                    "reason": "先按群名搜索 chat_id。",
                    "expected": "search",
                    "final_response_hint": "读取最近消息并整理摘要。",
                    "reason_for_confirmation": "",
                }
            if group:
                chat_id = self._extract_chat_id_from_output(
                    execution_results[-1].get("stdout") or execution_results[-1].get("stderr") or ""
                )
                if chat_id:
                    return {
                        "done": False,
                        "summary": f"读取群【{group}】最近 {count} 条消息并整理摘要",
                        "command": f"lark-cli im +chat-messages-list --chat-id {self._quote_cli_arg(chat_id)} --page-size {count} --format json",
                        "reason": "已获取群 chat_id，继续读取最近消息。",
                        "expected": "read",
                        "final_response_hint": "提炼消息摘要。",
                        "reason_for_confirmation": "",
                    }
                return {"done": True, "summary": "群搜索未解析到 chat_id，停止读取消息。"}
            return None

        group_create = self._parse_group_create_request(normalized)
        if group_create:
            group_name, members = group_create
            resolved_ids: List[str] = []
            for item in execution_results:
                resolved = self._extract_open_id_from_output(item.get("stdout") or item.get("stderr") or "")
                if resolved and resolved not in resolved_ids:
                    resolved_ids.append(resolved)

            if len(execution_results) < len(members):
                next_member = members[len(execution_results)]
                return {
                    "done": False,
                    "summary": f"创建群《{group_name}》，并拉入：{', '.join(['我', *members])}",
                    "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(next_member)}",
                    "reason": f"继续搜索联系人 {next_member}，获取 open_id。",
                    "expected": "search",
                    "final_response_hint": "收集完所有成员 open_id 后创建群聊。",
                    "reason_for_confirmation": "该请求会创建群聊并邀请成员，属于写操作。",
                }

            if len(resolved_ids) >= len(members):
                users = ",".join(resolved_ids[: len(members)])
                return {
                    "done": False,
                    "summary": f"创建群《{group_name}》，并拉入：{', '.join(['我', *members])}",
                    "command": f"lark-cli im +chat-create --name {self._quote_cli_arg(group_name)} --users {self._quote_cli_arg(users)} --as user",
                    "reason": "已获取所有成员 open_id，使用 user 身份一次性建群并邀请成员。",
                    "expected": "write",
                    "final_response_hint": "总结群聊是否创建成功，并返回 chat_id。",
                    "reason_for_confirmation": "该请求会创建群聊并邀请成员，属于写操作。",
                }

            return {"done": True, "summary": "未能收集齐所有成员 open_id，停止建群。"}

        calendar_create = self._build_calendar_create_plan(normalized)
        attendees = self._parse_attendees(normalized)
        if calendar_create and attendees:
            target_group = self._parse_calendar_link_target_group(normalized)
            resolved_ids: List[str] = []
            for item in execution_results:
                resolved = self._extract_open_id_from_output(item.get("stdout") or item.get("stderr") or "")
                if resolved and resolved not in resolved_ids:
                    resolved_ids.append(resolved)

            if len(resolved_ids) < len(attendees):
                next_attendee = attendees[len(resolved_ids)]
                return {
                    "done": False,
                    "summary": calendar_create["summary"],
                    "command": f"lark-cli contact +search-user --query {self._quote_cli_arg(next_attendee)}",
                    "reason": f"继续搜索参会人 {next_attendee}，获取 open_id。",
                    "expected": "search",
                    "final_response_hint": "收集完参会人 open_id 后创建会议日程。",
                    "reason_for_confirmation": "该请求会创建飞书日程并邀请参与人，属于写操作。",
                }

            calendar_event: Optional[Dict[str, Any]] = None
            for item in execution_results:
                if (
                    item.get("success")
                    and str(item.get("command", "")).startswith("lark-cli calendar +create ")
                ):
                    calendar_event = item
                    break

            if len(resolved_ids) >= len(attendees):
                if not calendar_event:
                    summary = self._extract_calendar_summary(normalized)
                    start, end = self._parse_calendar_range(normalized) or self._tomorrow_three_pm_range()
                    attendee_ids = ",".join(resolved_ids[: len(attendees)])
                    return {
                        "done": False,
                        "summary": calendar_create["summary"],
                        "command": (
                            f"lark-cli calendar +create --summary {self._quote_cli_arg(summary)} "
                            f"--start {self._quote_cli_arg(start)} --end {self._quote_cli_arg(end)} "
                            f"--attendee-ids {self._quote_cli_arg(attendee_ids)} --as user"
                        ),
                        "reason": "已获取参会人 open_id，使用标准 ISO 时间创建会议日程并邀请参与人。",
                        "expected": "write",
                        "final_response_hint": "总结日程是否创建成功，并返回 event_id 等关键结果。",
                        "reason_for_confirmation": "该请求会创建飞书日程并邀请参与人，属于写操作。",
                    }

                if target_group:
                    chat_id = None
                    for item in execution_results:
                        if item.get("success") and str(item.get("command", "")).startswith("lark-cli im +chat-search "):
                            chat_id = self._extract_chat_id_from_output(item.get("stdout") or item.get("stderr") or "")
                            if chat_id:
                                break

                    sent_to_group = any(
                        item.get("success")
                        and str(item.get("command", "")).startswith("lark-cli im +messages-send ")
                        and "--chat-id" in str(item.get("command", ""))
                        for item in execution_results
                    )
                    if sent_to_group:
                        return {"done": True, "summary": "会议日程已创建，会议信息也已发送到目标群。"}

                    if not chat_id:
                        return {
                            "done": False,
                            "summary": calendar_create["summary"],
                            "command": f"lark-cli im +chat-search --query {self._quote_cli_arg(target_group)} --format json",
                            "reason": f"日程已创建，继续搜索群【{target_group}】的 chat_id，以便发送会议信息。",
                            "expected": "search",
                            "final_response_hint": "找到群 chat_id 后发送会议链接或日程信息。",
                            "reason_for_confirmation": "该请求会向飞书群发送消息，属于写操作。",
                        }

                    event_output = calendar_event.get("stdout") or calendar_event.get("stderr") or ""
                    event_url = self._extract_url_from_output(event_output)
                    event_id = self._extract_event_id_from_output(event_output) or "未知"
                    message = (
                        f"需求评审会已创建。会议链接：{event_url}"
                        if event_url
                        else f"需求评审会已创建。当前 CLI 未返回可直接打开的会议链接，日程 ID：{event_id}。"
                    )
                    return {
                        "done": False,
                        "summary": calendar_create["summary"],
                        "command": f"lark-cli im +messages-send --chat-id {self._quote_cli_arg(chat_id)} --text {self._quote_lark_text_arg(message)}",
                        "reason": "已获取目标群 chat_id，继续发送会议链接或日程信息，完成用户要求的后置通知。",
                        "expected": "write",
                        "final_response_hint": "总结日程创建和群通知是否都成功。",
                        "reason_for_confirmation": "该请求会向飞书群发送消息，属于写操作。",
                    }

                return {"done": True, "summary": "会议日程已创建完成。"}

            return {"done": True, "summary": "未能收集齐所有参会人 open_id，停止创建会议日程。"}

        return None

    def _select_relevant_skills(self, query: str, limit: int = 4) -> List[SkillDoc]:
        query_lower = query.lower()
        scored: List[Tuple[int, SkillDoc]] = []
        for doc in self._skills_metadata.values():
            haystack = " ".join(
                [
                    doc.key.lower(),
                    doc.name.lower(),
                    doc.description.lower(),
                    doc.content[:1500].lower(),
                ]
            )
            score = 0
            for token in self._tokenize(query_lower):
                if token in haystack:
                    score += 3
                if token in doc.key.lower():
                    score += 3
                if token in doc.description.lower():
                    score += 2
            for hint in SKILL_KEYWORD_HINTS.get(doc.key, ()):
                if hint.lower() in query_lower:
                    score += 8
            if doc.key == "lark-reliable-scenes":
                score += 6
            if doc.key == "lark-shared":
                score += 100
            scored.append((score, doc))

        scored.sort(key=lambda item: (-item[0], item[1].key))
        selected = [doc for score, doc in scored if score > 0][:limit]
        if not any(doc.key == "lark-shared" for doc in selected) and "lark-shared" in self._skills_metadata:
            selected.insert(0, self._skills_metadata["lark-shared"])
        selected = selected[:limit]
        return self._expand_related_skills(selected, limit=max(limit, 6))

    def _expand_related_skills(self, selected_skills: List[SkillDoc], limit: int = 6) -> List[SkillDoc]:
        expanded: List[SkillDoc] = []
        seen: set[str] = set()

        def push(skill_doc: SkillDoc) -> None:
            if skill_doc.key in seen or len(expanded) >= limit:
                return
            expanded.append(skill_doc)
            seen.add(skill_doc.key)

        for doc in selected_skills:
            push(doc)

        for doc in list(expanded):
            for related_key in self._extract_related_skill_keys(doc):
                related_doc = self._skills_metadata.get(related_key)
                if related_doc:
                    push(related_doc)

        if "lark-shared" in self._skills_metadata:
            push(self._skills_metadata["lark-shared"])
        if "lark-reliable-scenes" in self._skills_metadata:
            push(self._skills_metadata["lark-reliable-scenes"])
        return expanded[:limit]

    @staticmethod
    def _preview_command_output(stdout: str, stderr: str, limit: int = 280) -> str:
        output = (stdout or stderr or "").strip()
        if not output:
            return "无输出"
        output = re.sub(r"\s+", " ", output)
        return output[:limit] + ("..." if len(output) > limit else "")

    @staticmethod
    def _make_progress_update(content: str) -> Dict[str, Any]:
        return {"type": "progress", "content": content.rstrip()}

    async def _repair_failed_command(
        self,
        context: SkillContext,
        original_command: str,
        error_message: str,
        query: str,
        selected_skills: List[SkillDoc],
        references: List[ReferenceDoc],
        execution_results: List[Dict[str, Any]],
        cli_state: LarkCLIState,
    ) -> Optional[str]:
        missing_scopes = self._extract_missing_scopes(error_message)
        if missing_scopes:
            return None
        if re.search(r"profile\s+['\"]?[^'\"\s]+['\"]?\s+not found|available profiles", error_message, re.IGNORECASE):
            return None
        if re.search(r"\s--profile(?:\s|=)", original_command) and cli_state.profile:
            profile_pattern = rf"\s--profile(?:\s+|=){re.escape(cli_state.profile)}(?:\s|$)"
            if not re.search(profile_pattern, original_command):
                return None

        if (
            original_command.startswith("lark-cli im +messages-send ")
            and " --content " in original_command
            and re.search(r"(msg-?type|interactive|content|不匹配|conflict|mismatch)", error_message, re.IGNORECASE)
        ):
            chat_match = re.search(r'--chat-id\s+(?:"([^"]+)"|(\S+))', original_command)
            chat_id = (chat_match.group(1) or chat_match.group(2)) if chat_match else ""
            if chat_id:
                message = "会议已创建，请查看飞书日历中的会议详情。"
                for item in reversed(execution_results):
                    if item.get("success") and (
                        str(item.get("command", "")).startswith("lark-cli calendar +create ")
                        or str(item.get("command", "")).startswith("lark-cli calendar events create ")
                    ):
                        output = item.get("stdout") or item.get("stderr") or ""
                        meeting_url = self._extract_meeting_url_from_output(output)
                        meeting_number = self._extract_meeting_number_from_output(output)
                        event_url = self._extract_url_from_output(output)
                        event_id = self._extract_event_id_from_output(output) or ""
                        if meeting_url:
                            lines = ["会议已创建"]
                            if meeting_number:
                                lines.append(f"会议号：{meeting_number}")
                            lines.append(f"会议链接：{meeting_url}")
                            if event_id:
                                lines.append(f"日程 ID：{event_id}")
                            message = "\n".join(lines)
                        elif event_url:
                            message = f"会议已创建：{event_url}"
                        elif event_id:
                            message = f"会议已创建。日程 ID：{event_id}"
                        break
                return (
                    f"lark-cli im +messages-send --chat-id {self._quote_cli_arg(chat_id)} "
                    f"--text {self._quote_lark_text_arg(message)} --as user"
                )

        deterministic_repair = self._repair_command(original_command)
        if deterministic_repair and deterministic_repair != self._normalize_command(original_command):
            return deterministic_repair

        if not self.client:
            return None

        system_prompt = (
            "你是飞书 CLI 命令修复专家。根据失败的命令和错误信息，生成修复后的命令。\n"
            "\n"
            "修复规则：\n"
            "1. 仔细分析错误信息，理解命令失败的原因\n"
            "2. 检查命令格式、参数名称、参数值是否正确\n"
            "3. 确保使用正确的子命令和参数\n"
            "4. 时间参数必须使用 ISO 8601 格式（如：2024-06-15T00:00:00）\n"
            "5. 不要使用不存在的参数（如 --date）\n"
            "6. 本地 Excel/CSV 导入为多维表格时，优先修复为 lark-cli drive +import --file <path> --type bitable --name <title>\n"
            "7. 保持命令的核心意图不变\n"
            "\n"
            "输出格式：\n"
            "只输出修复后的命令，不要包含任何解释或其他内容。\n"
            "如果无法修复，输出空字符串。"
        )

        user_prompt = (
            f"原始命令：{original_command}\n"
            f"错误信息：{error_message}\n"
            f"用户需求：{query}\n"
            f"\n"
            f"已执行的命令和结果：\n"
        )

        for i, result in enumerate(execution_results[-5:], 1):
            status = "成功" if result["success"] else "失败"
            user_prompt += (
                f"{i}. 命令：{result['command']}\n"
                f"   状态：{status}\n"
                f"   输出：{result['stdout'] or result['stderr'] or '无输出'}\n"
            )

        user_prompt += "\n请根据以上信息生成修复后的命令："

        repaired_command = await self._run_llm_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=500,
        )

        if not repaired_command:
            return None

        repaired_command = self._repair_command(repaired_command)
        if not repaired_command or not repaired_command.startswith("lark-cli "):
            return None

        if repaired_command == original_command:
            return None

        return repaired_command

    async def _run_llm_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> Optional[Dict[str, Any]]:
        if not self.client:
            return None

        def invoke() -> str:
            if self.settings.LLM_PROVIDER == "anthropic":
                response = self.client.messages.create(
                    model=self.settings.LLM_MODEL,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                return "".join(
                    block.text for block in response.content if getattr(block, "type", "") == "text"
                )

            response = self.client.chat.completions.create(
                model=self.settings.LLM_MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return response.choices[0].message.content or ""

        try:
            raw_text = await asyncio.wait_for(asyncio.to_thread(invoke), timeout=self._llm_timeout)
        except asyncio.TimeoutError:
            print("Warning: lark-cli llm request timed out, using fallback path")
            return None
        except Exception as exc:
            print(f"Warning: lark-cli llm request failed: {exc}")
            return None
        return self._extract_json_payload(raw_text)

    async def _run_llm_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> Optional[str]:
        if not self.client:
            return None

        def invoke() -> str:
            if self.settings.LLM_PROVIDER == "anthropic":
                response = self.client.messages.create(
                    model=self.settings.LLM_MODEL,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                return "".join(
                    block.text for block in response.content if getattr(block, "type", "") == "text"
                )

            response = self.client.chat.completions.create(
                model=self.settings.LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return response.choices[0].message.content or ""

        try:
            raw_text = await asyncio.wait_for(asyncio.to_thread(invoke), timeout=self._llm_timeout)
        except asyncio.TimeoutError:
            print("Warning: lark-cli llm summary timed out, using fallback summary")
            return None
        except Exception as exc:
            print(f"Warning: lark-cli llm summary failed: {exc}")
            return None
        return raw_text.strip() or None

    def _select_references(self, query: str, selected_skills: Iterable[SkillDoc], limit: int = 6) -> List[ReferenceDoc]:
        query_lower = query.lower()
        candidates: List[Tuple[int, ReferenceDoc]] = []
        for doc in selected_skills:
            for ref in doc.references:
                text = f"{ref.path.stem} {ref.title} {ref.content[:1800]}".lower()
                score = 0
                for token in self._tokenize(query_lower):
                    if token in text:
                        score += 2
                    if token in ref.path.stem.lower():
                        score += 3
                if score > 0:
                    candidates.append((score, ref))

        candidates.sort(key=lambda item: (-item[0], str(item[1].path)))
        dedup: List[ReferenceDoc] = []
        seen: set[str] = set()
        for _, ref in candidates:
            if str(ref.path) in seen:
                continue
            dedup.append(ref)
            seen.add(str(ref.path))
            if len(dedup) >= limit:
                break
        return dedup

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        return [token for token in re.split(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", text) if len(token) >= 2]

    def _is_write_request(self, query: str, commands: Optional[List[str]] = None) -> bool:
        query_lower = query.lower()
        if any(keyword in query_lower for keyword in WRITE_KEYWORDS):
            return True
        if commands:
            for command in commands:
                if any(keyword in command.lower() for keyword in WRITE_KEYWORDS):
                    return True
        return False

    def _build_planning_messages(
        self,
        context: SkillContext,
        query: str,
        selected_skills: List[SkillDoc],
        references: List[ReferenceDoc],
        cli_state: LarkCLIState,
    ) -> List[Dict[str, str]]:
        skill_blocks = []
        for doc in selected_skills:
            ref_names = ", ".join(ref.path.name for ref in doc.references[:8])
            skill_blocks.append(
                f"[{doc.key}]\n"
                f"name: {doc.name}\n"
                f"description: {doc.description}\n"
                f"cli_help: {doc.cli_help}\n"
                f"rules:\n{doc.content[:3000]}\n"
                f"references: {ref_names}\n"
            )

        ref_blocks = []
        for ref in references:
            ref_blocks.append(
                f"[{ref.path.name}] from {ref.path.parent.name}\n"
                f"{ref.content[:2600]}\n"
            )

        system_prompt = (
            "你是飞书 CLI 编排器。你必须严格依据提供的技能 markdown 规则生成计划。\n"
            "要求：\n"
            "1. 优先使用 shortcut，如 `lark-cli <domain> +<verb>`。\n"
            "2. 如果要调用原生 API，必须先执行对应的 `lark-cli schema ...`。\n"
            "3. 写操作（发送、创建、更新、删除、上传等）只有在用户意图明确时才允许执行。\n"
            "4. 不要编造参数名；拿不准时先给出探索命令或 schema 命令。\n"
            "5. 本地终端探测到 lark-cli 已安装/已配置/已登录时，不要重复规划安装、config init、auth login、auth status。\n"
            "6. 输出必须是 JSON，不要带 Markdown 代码块。\n"
            "7. JSON 结构："
            '{"summary":"",'
            '"relevant_skills":[""],'
            '"references":[""],'
            '"need_confirmation":true,'
            '"reason_for_confirmation":"",'
            '"commands":[{"command":"","reason":"","expected":"read|write|schema|search"}],'
            '"final_response_hint":""}'
        )

        user_prompt = (
            f"用户问题：{query}\n\n"
            f"最近对话：{json.dumps(context.history[-6:], ensure_ascii=False)}\n\n"
            f"{self._build_cli_state_text(cli_state)}\n\n"
            "候选技能文档：\n"
            + "\n\n".join(skill_blocks)
            + "\n\n候选 reference：\n"
            + "\n\n".join(ref_blocks)
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _build_step_messages(
        self,
        context: SkillContext,
        query: str,
        selected_skills: List[SkillDoc],
        references: List[ReferenceDoc],
        execution_results: List[Dict[str, Any]],
        cli_state: LarkCLIState,
    ) -> List[Dict[str, str]]:
        skill_blocks = []
        for doc in selected_skills:
            related = ", ".join(self._extract_related_skill_keys(doc))
            skill_blocks.append(
                f"[{doc.key}]\n"
                f"name: {doc.name}\n"
                f"description: {doc.description}\n"
                f"cli_help: {doc.cli_help}\n"
                f"related_skills: {related or 'none'}\n"
                f"rules:\n{self._truncate_text(doc.content, 2200)}\n"
            )

        ref_blocks = []
        for ref in references[:8]:
            ref_blocks.append(
                f"[{ref.path.name}] from {ref.path.parent.name}\n"
                f"{self._truncate_text(ref.content, 1800)}\n"
            )

        history_blocks = []
        for index, item in enumerate(execution_results, start=1):
            output = item.get("stdout") or item.get("stderr") or ""
            history_blocks.append(
                f"step {index}\n"
                f"command: {item.get('command')}\n"
                f"expected: {item.get('expected')}\n"
                f"success: {item.get('success')}\n"
                f"reason: {item.get('reason')}\n"
                f"output:\n{self._truncate_text(output, 1200)}\n"
            )

        system_prompt = (
            "你是飞书 CLI 的逐步执行规划器。你必须严格遵守提供的 skill markdown 规则，"
            "每次只返回下一步一个命令，或者返回 done=true 表示任务完成。\n"
            "规则：\n"
            "1. 优先使用 shortcut 命令。\n"
            "2. 需要跨 skill 串联时，使用上一步结果中的真实 token/id 继续生成下一步命令。\n"
            "3. 禁止编造占位符，如 ou_xxx、oc_xxx、doxxx；如果没有真实值，就先搜索/查询。\n"
            "4. 如需原生 API，必须先 schema。\n"
            "5. 如果本地终端已显示 configured/authenticated 为 true，就不要再输出 config init、auth login、auth status 这类命令。\n"
            "6. 只有用户原始需求中的每个动作都完成后才能返回 done=true；如果用户要求创建后再发送、通知、转发、分享，不能在第一个写操作成功后结束。\n"
            "7. 自动修复失败命令时最多尝试 3 次，仍失败就停止并暴露失败信息。\n"
            "8. 本地 Excel/CSV 导入 Base/多维表格必须使用 drive +import --type bitable；如果导入返回 next_command，要继续执行 task_result 获取最终结果。\n"
            "9. 对包含多个动作的原始需求必须逐项闭环，尤其是创建/导入/上传后再发送到群。\n"
            "10. 找多人共同会议时间时，优先使用 calendar +suggestion --attendee-ids；群可以直接用 oc_ chat_id，不要先猜 chat.members.get 参数逐个试错。\n"
            "11. 如果用户要求会议号/会议链接/会议详情发群，必须创建带 vchat.vc_type=vc 的日程并从 meeting_url 提取会议号；只发送 event_id 不算完成。\n"
            "12. 群会议通知优先用 im +messages-send --text 发送会议号、会议链接、时间和参与人；通知文本保持单行干净格式，用中文分号分隔字段，不要包含字面量 \\n，不要手写 interactive 卡片 JSON，也不要依赖 Markdown 解析。\n"
            "13. 输出必须是 JSON，不要 Markdown。\n"
            'JSON 结构：{"done":false,"summary":"","command":"","reason":"","expected":"read|write|schema|search","final_response_hint":"","reason_for_confirmation":""}'
        )

        user_prompt = (
            f"用户问题：{query}\n\n"
            f"最近对话：{json.dumps(context.history[-6:], ensure_ascii=False)}\n\n"
            f"{self._build_cli_state_text(cli_state)}\n\n"
            "候选技能文档：\n"
            + "\n\n".join(skill_blocks)
            + "\n\n候选 references：\n"
            + "\n\n".join(ref_blocks)
            + "\n\n已有执行结果：\n"
            + ("\n\n".join(history_blocks) if history_blocks else "无")
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    async def _plan_with_llm(
        self,
        context: SkillContext,
        query: str,
        selected_skills: List[SkillDoc],
        references: List[ReferenceDoc],
        cli_state: LarkCLIState,
    ) -> Optional[Dict[str, Any]]:
        if not self.client:
            return None

        messages = self._build_planning_messages(context, query, selected_skills, references, cli_state)
        return await self._run_llm_json(
            system_prompt=messages[0]["content"],
            user_prompt=messages[1]["content"],
            max_tokens=2500,
        )

    async def _normalize_query_with_agent(
        self,
        context: SkillContext,
        query: str,
        cli_state: LarkCLIState,
    ) -> Optional[NormalizedIntent]:
        if not self.client:
            return None

        system_prompt = (
            "你是飞书 CLI 意图规范化 agent。你的任务不是直接生成命令，"
            "而是把用户的自然语言请求改写成更稳定、更标准的中文意图表达，"
            "以便后续规则引擎命中。\n"
            "要求：\n"
            "1. 保留原始人名、群名、标题、时间、内容，不要改写实体。\n"
            "2. 只做意图规范化，不要补充不存在的参数或命令。\n"
            "3. 优先输出以下几类规范表达之一：\n"
            '   - 给{用户}发消息：{内容}\n'
            '   - 给【{群名}】发消息：{内容}\n'
            '   - 创建会议日程：{时间描述}，参与人：{人名列表}\n'
            '   - 创建群《{群名}》，成员包括：{成员列表}\n'
            '   - 创建文档《{标题}》：{内容或占位说明}\n'
            '   - 创建多维表格《{标题}》，并写入：{数据来源}\n'
            '   - 导入本地文件{file_path}为多维表格《{标题}》，并发送到【{群名}】\n'
            '   - 查看群【{群名}】下周忙闲，找{时长}会议时间并发送会议卡片\n'
            "4. 如果无法确定，就返回原句。\n"
            '5. 输出 JSON：{"normalized_query":"","intent_type":""}'
        )
        user_prompt = (
            f"用户原始请求：{query}\n\n"
            f"最近对话：{json.dumps(context.history[-6:], ensure_ascii=False)}\n\n"
            f"{self._build_cli_state_text(cli_state)}"
        )
        parsed = await self._run_llm_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=500,
        )
        if not parsed:
            return None

        normalized_query = self._sanitize_query_text(parsed.get("normalized_query") or "")
        if not normalized_query or normalized_query == query:
            return None
        return NormalizedIntent(
            normalized_query=normalized_query,
            intent_type=str(parsed.get("intent_type") or "").strip(),
        )

    async def _plan_next_step_with_llm(
        self,
        context: SkillContext,
        query: str,
        selected_skills: List[SkillDoc],
        references: List[ReferenceDoc],
        execution_results: List[Dict[str, Any]],
        cli_state: LarkCLIState,
    ) -> Optional[Dict[str, Any]]:
        if not self.client:
            return None

        messages = self._build_step_messages(
            context=context,
            query=query,
            selected_skills=selected_skills,
            references=references,
            execution_results=execution_results,
            cli_state=cli_state,
        )
        parsed = await self._run_llm_json(
            system_prompt=messages[0]["content"],
            user_prompt=messages[1]["content"],
            max_tokens=1500,
        )
        if parsed is None:
            return None
        parsed["expected"] = self._normalize_expected_type(parsed.get("expected"))
        parsed["command"] = self._normalize_command(parsed.get("command", ""))
        return parsed

    def _fallback_plan(
        self,
        query: str,
        selected_skills: List[SkillDoc],
        references: List[ReferenceDoc],
    ) -> Dict[str, Any]:
        doc_create_plan = self._build_doc_create_plan(query)
        if doc_create_plan:
            return doc_create_plan

        commands: List[Dict[str, str]] = []
        query_lower = query.lower()

        if (
            "message" in query_lower
            or "send" in query_lower
            or "消息" in query
            or "发给" in query
            or "发个消息" in query
            or "告诉" in query
        ) and "lark-im" in self._skills_metadata:
            commands.append(
                {
                    "command": "lark-cli contact --help",
                    "reason": "消息发送前通常要先定位用户。",
                    "expected": "read",
                }
            )
            commands.append(
                {
                    "command": "lark-cli im --help",
                    "reason": "确认 im 域消息发送能力和参数格式。",
                    "expected": "read",
                }
            )
        elif ("doc" in query_lower or "wiki" in query_lower) and "lark-doc" in self._skills_metadata:
            commands.append(
                {
                    "command": "lark-cli docs --help",
                    "reason": "先查看 docs 域可用能力。",
                    "expected": "read",
                }
            )
        else:
            commands.append(
                {
                    "command": "lark-cli --help",
                    "reason": "先总览 lark-cli 可用域和命令。",
                    "expected": "read",
                }
            )

        return {
            "summary": "已根据本地技能文档生成保守计划。",
            "relevant_skills": [doc.key for doc in selected_skills],
            "references": [ref.path.name for ref in references],
            "need_confirmation": self._is_write_request(query),
            "reason_for_confirmation": "该请求可能涉及写操作，需要确认后再执行。",
            "commands": commands,
            "final_response_hint": "如果帮助信息不足，再根据输出继续细化。",
        }

    async def _plan_commands(
        self, context: SkillContext, query: str, cli_state: LarkCLIState
    ) -> Tuple[Dict[str, Any], List[SkillDoc], List[ReferenceDoc]]:
        query = self._sanitize_query_text(query)
        selected_skills = self._select_relevant_skills(query)
        references = self._select_references(query, selected_skills)
        plan = self._build_heuristic_plan(query)
        normalized_intent: Optional[NormalizedIntent] = None
        if not plan:
            normalized_intent = await self._normalize_query_with_agent(context, query, cli_state)
            if normalized_intent:
                plan = self._build_heuristic_plan(normalized_intent.normalized_query)
        if not plan:
            plan = await self._plan_with_llm(context, query, selected_skills, references, cli_state)
        if not plan:
            plan = self._fallback_plan(query, selected_skills, references)
        plan_skill_keys = [
            key for key in plan.get("relevant_skills", [])
            if isinstance(key, str) and key in self._skills_metadata
        ]
        if plan_skill_keys:
            selected_by_key = {doc.key: doc for doc in selected_skills}
            for key in plan_skill_keys:
                selected_by_key.setdefault(key, self._skills_metadata[key])
            selected_skills = self._expand_related_skills(list(selected_by_key.values()), limit=8)
            references = self._select_references(query, selected_skills)
        if normalized_intent:
            plan.setdefault("normalized_query", normalized_intent.normalized_query)
            if normalized_intent.intent_type:
                plan.setdefault("intent_type", normalized_intent.intent_type)
        plan.setdefault("relevant_skills", [doc.key for doc in selected_skills])
        plan.setdefault("references", [ref.path.name for ref in references])
        plan.setdefault("commands", [])
        return plan, selected_skills, references

    def _build_fallback_step(
        self,
        query: str,
        selected_skills: List[SkillDoc],
        references: List[ReferenceDoc],
        execution_results: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if execution_results:
            return {"done": True, "summary": "已完成保守模式下的可执行步骤。"}

        plan = self._fallback_plan(query, selected_skills, references)
        commands = plan.get("commands", [])
        if not commands:
            return {"done": True, "summary": plan.get("summary", "")}

        first = commands[0]
        return {
            "done": False,
            "summary": plan.get("summary", ""),
            "command": self._normalize_command(first.get("command", "")),
            "reason": first.get("reason", ""),
            "expected": self._normalize_expected_type(first.get("expected")),
            "final_response_hint": plan.get("final_response_hint", ""),
            "reason_for_confirmation": plan.get("reason_for_confirmation", ""),
        }

    def _format_confirmation_message(
        self,
        query: str,
        plan: Dict[str, Any],
        selected_skills: List[SkillDoc],
        references: List[ReferenceDoc],
    ) -> str:
        lines = [
            "这个请求会触发飞书写操作，我先把执行计划给你确认：",
            f"- 需求：{query}",
            f"- 计划摘要：{plan.get('summary', '未提供')}",
            f"- 相关技能：{', '.join(plan.get('relevant_skills') or [doc.key for doc in selected_skills])}",
        ]
        if references:
            lines.append(f"- 参考文档：{', '.join(ref.path.name for ref in references[:6])}")
        for item in plan.get("commands", [])[:6]:
            command = self._normalize_command(item.get("command", ""))
            if command:
                lines.append(f"- 待执行：`{command}`")
        reason = plan.get("reason_for_confirmation")
        if reason:
            lines.append(f"- 原因：{reason}")
        lines.append("如果确认执行，请重试并传入 `confirm_write=true`。")
        return "；".join(lines)

    def _build_confirmation_plan(
        self,
        query: str,
        selected_skills: List[SkillDoc],
        references: List[ReferenceDoc],
        step: Dict[str, Any],
        execution_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        commands: List[Dict[str, str]] = []
        for item in execution_results:
            commands.append(
                {
                    "command": item.get("command", ""),
                    "reason": item.get("reason", ""),
                    "expected": item.get("expected", ""),
                }
            )
        commands.append(
            {
                "command": self._repair_command(step.get("command", "")),
                "reason": step.get("reason", ""),
                "expected": step.get("expected", "write"),
            }
        )

        return {
            "summary": step.get("summary") or f"准备继续处理：{query}",
            "relevant_skills": [doc.key for doc in selected_skills],
            "references": [ref.path.name for ref in references],
            "reason_for_confirmation": step.get("reason_for_confirmation") or "接下来将执行飞书写操作。",
            "commands": commands,
        }

    async def _summarize_execution(
        self,
        query: str,
        plan: Dict[str, Any],
        execution_results: List[Dict[str, Any]],
    ) -> str:
        if not self.client:
            return self._fallback_summary(plan, execution_results)

        payload = {
            "query": query,
            "summary": plan.get("summary"),
            "hint": plan.get("final_response_hint"),
            "results": execution_results,
        }
        system_prompt = (
            "你是飞书 CLI 执行结果总结器。请基于给定 JSON 输出中文总结，"
            "说明做了什么、成功失败、关键返回值、下一步建议。"
            "不要编造结果，不要输出 Markdown 代码块。"
        )
        summary = await self._run_llm_text(
            system_prompt=system_prompt,
            user_prompt=json.dumps(payload, ensure_ascii=False),
            max_tokens=1200,
        )
        return summary or self._fallback_summary(plan, execution_results)

    @staticmethod
    def _fallback_summary(plan: Dict[str, Any], execution_results: List[Dict[str, Any]]) -> str:
        lines = [plan.get("summary", "飞书 CLI 执行完成。")] 
        for item in execution_results:
            status = "成功" if item.get("success") else "失败"
            lines.append(f"- {status}: `{item.get('command')}`")
            output = (item.get("stdout") or item.get("stderr") or "").strip()
            if output:
                lines.append(output[:500])
        return "\n".join(lines)

    async def _ensure_cli_ready(
        self,
        user_id: str = "",
        require_user_auth: bool = True,
        expected_user_name: str = "",
    ) -> Tuple[bool, str]:
        if expected_user_name:
            cli_state = await self._probe_cli_state(user_id, expected_user_name)
        else:
            cli_state = await self._probe_cli_state(user_id)
        if not cli_state.installed:
            return False, f"{cli_state.install_info}\n\n{self.get_install_guide()}"
        if not cli_state.configured:
            return False, f"{cli_state.config_info}\n\n{self._build_user_setup_hint(cli_state)}"
        if require_user_auth and not cli_state.authenticated:
            return False, f"{cli_state.auth_info}\n\n{self._build_user_setup_hint(cli_state)}"
        return True, ""

    @staticmethod
    def _build_user_setup_hint(cli_state: LarkCLIState) -> str:
        if not cli_state.profile:
            return (
                "飞书 CLI 未就绪，请先完成配置和授权：\n"
                "1. `lark-cli config init --new`\n"
                "2. `lark-cli auth login --recommend`\n"
                "3. `lark-cli auth status`"
            )
        return (
            "当前登录用户需要先完成独立的飞书 CLI 配置和授权：\n"
            "1. `lark-cli config init --new`\n"
            "2. `lark-cli auth login --recommend`\n"
            "3. `lark-cli auth status`\n"
            f"系统会在后台把这些命令隔离到当前登录用户的 CLI 环境：{cli_state.profile}"
        )

    async def _execute_workflow(
        self,
        context: SkillContext,
        *,
        command: Optional[str],
        query: str,
        timeout: int,
        confirm_write: bool,
        stream: bool,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        expected_user_name = str((context.metadata or {}).get("employee_name") or "")
        cli_state = await self._probe_cli_state(context.user_id, expected_user_name)
        requires_user_auth = self._query_requires_user_auth(query, command)
        if not cli_state.installed or not cli_state.configured or (requires_user_auth and not cli_state.authenticated):
            ready, ready_message = await self._ensure_cli_ready(context.user_id, requires_user_auth, expected_user_name)
        else:
            ready, ready_message = True, ""
        if not ready:
            yield {
                "kind": "final",
                "result": SkillResult(
                    success=False,
                    message=ready_message,
                    data=self._build_setup_metadata(cli_state),
                ),
            }
            return

        if command:
            normalized_command = self._normalize_command(command)
            if stream:
                yield self._make_progress_update(f"准备执行命令：`{normalized_command}`")
            success, stdout, stderr = await self.execute_command(normalized_command, timeout, context.user_id)
            message = stdout.strip() if success else (stderr.strip() or "命令执行失败")
            if stream:
                preview = self._preview_command_output(stdout, stderr)
                status = "成功" if success else "失败"
                yield self._make_progress_update(f"命令执行{status}：`{normalized_command}`\n返回摘要：{preview}")
            if not success:
                missing_scopes = self._extract_missing_scopes(stdout or stderr)
                if missing_scopes:
                    yield {
                        "kind": "final",
                        "result": SkillResult(
                            success=False,
                            message=f"当前飞书授权缺少权限：{', '.join(missing_scopes)}。请点击授权卡片补充授权后重试。",
                            data=self._build_scope_setup_metadata(cli_state, missing_scopes),
                        ),
                    }
                    return
                if self._is_user_authorization_error(stdout or stderr):
                    yield {
                        "kind": "final",
                        "result": SkillResult(
                            success=False,
                            message="当前系统账号还需要完成飞书用户身份授权。请点击授权卡片完成授权后重试。",
                            data=self._build_setup_metadata(cli_state),
                        ),
                    }
                    return
            yield {
                "kind": "final",
                "result": SkillResult(
                    success=success,
                    message=message,
                    data={"command": normalized_command, "stdout": stdout, "stderr": stderr},
                ),
            }
            return

        if not query:
            yield {
                "kind": "final",
                "result": SkillResult(success=False, message="请提供飞书操作需求或完整的 lark-cli 命令。"),
            }
            return

        initial_plan, selected_skills, references = await self._plan_commands(context, query, cli_state)
        execution_results: List[Dict[str, Any]] = []
        latest_plan = dict(initial_plan)
        seen_commands: set[str] = set()

        if stream:
            skill_names = ", ".join(doc.key for doc in selected_skills) or "未命中明确 skill"
            yield self._make_progress_update(f"已选技能：{skill_names}")
            if references:
                yield self._make_progress_update(
                    "参考文档：" + ", ".join(ref.path.name for ref in references[:6])
                )
            if latest_plan.get("summary"):
                yield self._make_progress_update(f"规划摘要：{latest_plan['summary']}")

        for step_index in range(1, MAX_LARK_CLI_STEPS + 1):
            if stream:
                yield self._make_progress_update(f"正在规划第 {step_index} 步...")

            step = self._build_heuristic_step(query, execution_results)
            if not step:
                step = await self._plan_next_step_with_llm(
                    context=context,
                    query=query,
                    selected_skills=selected_skills,
                    references=references,
                    execution_results=execution_results,
                    cli_state=cli_state,
                )
            if not step:
                step = self._build_fallback_step(
                    query=query,
                    selected_skills=selected_skills,
                    references=references,
                    execution_results=execution_results,
                )

            if not step:
                break

            latest_plan.update(
                {
                    "summary": step.get("summary") or latest_plan.get("summary", ""),
                    "final_response_hint": step.get("final_response_hint") or latest_plan.get("final_response_hint", ""),
                    "relevant_skills": [doc.key for doc in selected_skills],
                    "references": [ref.path.name for ref in references],
                }
            )

            if step.get("done"):
                if stream and step.get("summary"):
                    yield self._make_progress_update(f"规划器判定任务已完成：{step['summary']}")
                break

            current_command = self._normalize_command(step.get("command", ""))
            if not current_command:
                if stream:
                    yield self._make_progress_update("本轮没有生成有效命令，停止继续规划。")
                break

            if self._should_skip_bootstrap_command(current_command, cli_state):
                if stream:
                    yield self._make_progress_update(
                        f"已跳过冗余初始化命令：`{current_command}`"
                    )
                continue

            if current_command in seen_commands:
                if stream:
                    yield self._make_progress_update(
                        f"检测到重复命令，已停止循环：`{current_command}`"
                    )
                break
            seen_commands.add(current_command)

            if self._is_write_request(query, [current_command]) and not confirm_write:
                confirmation_plan = self._build_confirmation_plan(
                    query=query,
                    selected_skills=selected_skills,
                    references=references,
                    step=step,
                    execution_results=execution_results,
                )
                yield {
                    "kind": "final",
                    "result": SkillResult(
                        success=False,
                        message=self._format_confirmation_message(
                            query,
                            confirmation_plan,
                            selected_skills,
                            references,
                        ),
                        data={
                            "plan": confirmation_plan,
                            "executed_commands": execution_results,
                            "selected_skills": [doc.key for doc in selected_skills],
                            "references": [str(ref.path) for ref in references],
                        },
                        need_continue=True,
                    ),
                }
                return

            if stream:
                reason = step.get("reason", "") or "未提供原因"
                yield self._make_progress_update(
                    f"执行第 {step_index} 步：`{current_command}`\n原因：{reason}"
                )

            success, stdout, stderr = await self.execute_command(current_command, timeout, context.user_id)
            failed_result_index = len(execution_results)
            execution_results.append(
                {
                    "command": current_command,
                    "reason": step.get("reason", ""),
                    "expected": step.get("expected", "read"),
                    "success": success,
                    "stdout": stdout.strip(),
                    "stderr": stderr.strip(),
                }
            )
            latest_plan.setdefault("commands", [])
            latest_plan["commands"].append(
                {
                    "command": current_command,
                    "reason": step.get("reason", ""),
                    "expected": step.get("expected", "read"),
                }
            )

            if stream:
                status = "成功" if success else "失败"
                yield self._make_progress_update(
                    f"第 {step_index} 步执行{status}：`{current_command}`\n"
                    f"返回摘要：{self._preview_command_output(stdout, stderr)}"
                )

            if not success:
                missing_scopes = self._extract_missing_scopes(stdout or stderr)
                if missing_scopes:
                    if stream:
                        yield self._make_progress_update(
                            f"命令缺少飞书权限：{', '.join(missing_scopes)}。已生成补充授权步骤。"
                        )
                    yield {
                        "kind": "final",
                        "result": SkillResult(
                            success=False,
                            message=f"当前飞书授权缺少权限：{', '.join(missing_scopes)}。请点击授权卡片补充授权后重试。",
                            data={
                                "plan": latest_plan,
                                "executed_commands": execution_results,
                                "selected_skills": [doc.key for doc in selected_skills],
                                "references": [str(ref.path) for ref in references],
                                **self._build_scope_setup_metadata(cli_state, missing_scopes),
                            },
                        ),
                    }
                    return
                if self._is_user_authorization_error(stdout or stderr):
                    if stream:
                        yield self._make_progress_update("命令需要飞书用户身份授权。已生成授权步骤。")
                    yield {
                        "kind": "final",
                        "result": SkillResult(
                            success=False,
                            message="当前系统账号还需要完成飞书用户身份授权。请点击授权卡片完成授权后重试。",
                            data={
                                "plan": latest_plan,
                                "executed_commands": execution_results,
                                "selected_skills": [doc.key for doc in selected_skills],
                                "references": [str(ref.path) for ref in references],
                                **self._build_setup_metadata(cli_state),
                            },
                        ),
                    }
                    return
                retry_count = 0
                max_retries = 3
                last_failed_command = current_command
                last_error = stderr.strip() or "命令执行失败"

                while retry_count < max_retries:
                    retry_count += 1
                    if stream:
                        yield self._make_progress_update(
                            f"正在尝试第 {retry_count} 次自动修复..."
                        )

                    repaired_command = await self._repair_failed_command(
                        context=context,
                        original_command=last_failed_command,
                        error_message=last_error,
                        query=query,
                        selected_skills=selected_skills,
                        references=references,
                        execution_results=execution_results,
                        cli_state=cli_state,
                    )

                    if not repaired_command:
                        if stream:
                            yield self._make_progress_update(
                                f"第 {retry_count} 次修复失败，无法生成修复后的命令。"
                            )
                        break

                    if repaired_command in seen_commands:
                        if stream:
                            yield self._make_progress_update(
                                f"第 {retry_count} 次修复生成了重复命令，停止继续重试：`{repaired_command}`"
                            )
                        break
                    seen_commands.add(repaired_command)

                    if stream:
                        yield self._make_progress_update(
                            f"执行修复后的命令：`{repaired_command}`"
                        )

                    success, stdout, stderr = await self.execute_command(repaired_command, timeout, context.user_id)
                    execution_results.append(
                        {
                            "command": repaired_command,
                            "reason": f"自动修复（第 {retry_count} 次）",
                            "expected": step.get("expected", "read"),
                            "success": success,
                            "stdout": stdout.strip(),
                            "stderr": stderr.strip(),
                        }
                    )
                    latest_plan["commands"].append(
                        {
                            "command": repaired_command,
                            "reason": f"自动修复（第 {retry_count} 次）",
                            "expected": step.get("expected", "read"),
                        }
                    )

                    if stream:
                        status = "成功" if success else "失败"
                        yield self._make_progress_update(
                            f"修复后命令执行{status}：`{repaired_command}`\n"
                            f"返回摘要：{self._preview_command_output(stdout, stderr)}"
                        )

                    if success:
                        if 0 <= failed_result_index < len(execution_results):
                            execution_results[failed_result_index]["superseded_by_repair"] = repaired_command
                            execution_results[failed_result_index]["repair_attempts"] = retry_count
                            execution_results[failed_result_index]["repair_stdout"] = stdout.strip()
                        break

                    last_failed_command = repaired_command
                    last_error = stderr.strip() or "命令执行失败"

                if not success:
                    if stream:
                        yield self._make_progress_update(
                            f"命令执行失败，已尝试修复 {max_retries} 次仍无法成功。\n"
                            f"最后一次失败的命令：`{last_failed_command}`\n"
                            f"错误信息：{last_error}"
                        )
                    break

        if not execution_results and not latest_plan.get("summary"):
            yield {
                "kind": "final",
                "result": SkillResult(
                    success=False,
                    message="我已经读取了相关飞书技能文档，但这次没有规划出可执行命令。",
                    data={"plan": latest_plan},
                ),
            }
            return

        summary = await self._summarize_execution(query, latest_plan, execution_results)
        overall_success = bool(execution_results) and all(
            self._execution_result_counts_as_success(item) for item in execution_results
        )
        yield {
            "kind": "final",
            "result": SkillResult(
                success=overall_success,
                message=summary,
                data={
                    "plan": latest_plan,
                    "executed_commands": execution_results,
                    "selected_skills": [doc.key for doc in selected_skills],
                    "references": [str(ref.path) for ref in references],
                },
            ),
        }

    async def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        command = kwargs.get("command")
        query = self._sanitize_query_text(kwargs.get("query") or context.message)
        timeout = int(kwargs.get("timeout", DEFAULT_LARK_COMMAND_TIMEOUT) or DEFAULT_LARK_COMMAND_TIMEOUT)
        confirm_write = bool(kwargs.get("confirm_write", False))
        final_result: Optional[SkillResult] = None
        async for event in self._execute_workflow(
            context,
            command=command,
            query=query,
            timeout=timeout,
            confirm_write=confirm_write,
            stream=False,
        ):
            if event.get("kind") == "final":
                final_result = event["result"]
        return final_result or SkillResult(success=False, message="飞书 CLI 执行器未返回结果。")

    async def execute_stream(self, context: SkillContext, **kwargs) -> AsyncGenerator[Dict[str, Any], None]:
        command = kwargs.get("command")
        query = self._sanitize_query_text(kwargs.get("query") or context.message)
        timeout = int(kwargs.get("timeout", DEFAULT_LARK_COMMAND_TIMEOUT) or DEFAULT_LARK_COMMAND_TIMEOUT)
        confirm_write = bool(kwargs.get("confirm_write", False))
        yield self._make_progress_update(f"正在准备飞书 CLI 处理：{command or query}")
        async for event in self._execute_workflow(
            context,
            command=command,
            query=query,
            timeout=timeout,
            confirm_write=confirm_write,
            stream=True,
        ):
            if event.get("kind") == "final":
                result: SkillResult = event["result"]
                yield {"type": "content", "content": result.message}
                if result.data:
                    yield {"type": "metadata", "data": result.data}
            else:
                yield event


class LarkMessageSkill(BaseSkill):
    def __init__(self) -> None:
        self.lark_cli_skill = LarkCLISkill()

    @property
    def name(self) -> str:
        return "lark_message"

    @property
    def description(self) -> str:
        return "飞书消息发送技能。内部委托给 lark_cli，由其基于 markdown 规则完成联系人搜索和消息发送。"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_name": {"type": "string", "description": "收件人姓名"},
                "message": {"type": "string", "description": "消息内容"},
                "confirm_write": {"type": "boolean", "default": False},
            },
            "required": ["user_name", "message"],
        }

    async def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        user_name = kwargs.get("user_name")
        message = kwargs.get("message")
        confirm_write = bool(kwargs.get("confirm_write", False))
        query = (
            f"给飞书用户 {user_name} 发送消息，消息内容如下：{message}。"
            "请按技能文档规则先定位用户，再选择正确的发送方式。"
        )
        return await self.lark_cli_skill.execute(
            context,
            query=query,
            confirm_write=confirm_write,
        )

    async def execute_stream(self, context: SkillContext, **kwargs) -> AsyncGenerator[Dict[str, Any], None]:
        async for chunk in self.lark_cli_skill.execute_stream(
            context,
            query=(
                f"给飞书用户 {kwargs.get('user_name')} 发送消息，消息内容如下：{kwargs.get('message')}。"
                "请按技能文档规则先定位用户，再选择正确的发送方式。"
            ),
            confirm_write=bool(kwargs.get("confirm_write", False)),
        ):
            yield chunk
