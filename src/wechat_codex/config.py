from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit

import yaml

from .ilink_api import DEFAULT_API_BASE_URL, validate_base_url


def default_config_path() -> Path:
    """Return the config beside a frozen executable, or in the working directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.yaml"
    return Path("config.yaml")


@dataclass(frozen=True)
class AppConfig:
    source: Path
    ilink_api_base_url: str
    ilink_credentials_file: Path
    ilink_state_file: Path
    ilink_long_poll_timeout_seconds: float
    ilink_allowed_user_ids: frozenset[str]
    ilink_codex_user_ids: frozenset[str]
    response_prefix: str
    send_received_ack: bool
    received_ack_text: str
    poll_seconds: float
    max_reply_chars: int
    chatgpt_browser_channel: str
    chatgpt_headless: bool
    chatgpt_proxy_server: str | None
    chatgpt_conversation_title: str
    codex_command: str
    chat_timeout_seconds: int
    work_timeout_seconds: int
    default_project: str
    projects: dict[str, Path]
    account_runtime_dir: Path | None = None

    @property
    def runtime_dir(self) -> Path:
        return self.account_runtime_dir or self.source.parent / ".runtime"

    @property
    def chatgpt_profile_dir(self) -> Path:
        # The Plus browser profile is application-scoped even when a cloned
        # config points BridgeApp at one account's private runtime directory.
        return self.source.parent / ".runtime" / "chatgpt-plus-profile"


def _required_text(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"配置项 {key!r} 不能为空")
    return value


def _text_set(data: dict[str, Any], key: str) -> frozenset[str]:
    values = data.get(key, [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise ValueError(f"配置项 {key!r} 必须是字符串列表")
    cleaned: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"配置项 {key!r} 的第 {index + 1} 项必须是非空字符串")
        cleaned.add(value.strip())
    return frozenset(cleaned)


def _boolean(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError(f"配置项 {key!r} 必须是 true 或 false")


def _proxy_server(data: dict[str, Any]) -> str | None:
    value = data.get("chatgpt_proxy_server", "")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("配置项 'chatgpt_proxy_server' 必须是字符串")
    proxy = value.strip()
    if not proxy:
        return None
    if any(character.isspace() for character in proxy):
        raise ValueError("chatgpt_proxy_server 不能包含空白字符")
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    parsed = urlsplit(proxy)
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5"}:
        raise ValueError(
            "chatgpt_proxy_server 仅支持 http、https、socks4 或 socks5 代理"
        )
    if not parsed.hostname:
        raise ValueError("chatgpt_proxy_server 缺少有效的主机名")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("chatgpt_proxy_server 的端口无效") from exc
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("chatgpt_proxy_server 不能包含路径、查询参数或片段")
    return proxy


def _resolve_file(source: Path, value: Any, default: str) -> Path:
    candidate = Path(str(value or default)).expanduser()
    if not candidate.is_absolute():
        candidate = source.parent / candidate
    return candidate.resolve()


def load_config(path: str | Path) -> AppConfig:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"配置文件不存在：{source}")
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件顶层必须是 YAML 对象")

    project_values = raw.get("projects") or {}
    if not isinstance(project_values, dict) or not project_values:
        raise ValueError("至少需要配置一个 projects 项目")
    projects: dict[str, Path] = {}
    for alias, value in project_values.items():
        name = str(alias).strip()
        if not name:
            raise ValueError("项目别名不能为空")
        candidate = Path(str(value)).expanduser()
        if not candidate.is_absolute():
            candidate = source.parent / candidate
        projects[name] = candidate.resolve()
    default_project = str(raw.get("default_project") or next(iter(projects))).strip()
    if default_project not in projects:
        raise ValueError(f"default_project {default_project!r} 不在 projects 中")

    poll_value = raw.get("poll_seconds", 0.5)
    if isinstance(poll_value, bool):
        raise ValueError("poll_seconds 必须是有限数字")
    try:
        poll_seconds = float(poll_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("poll_seconds 必须是有限数字") from exc
    max_reply_chars = int(raw.get("max_reply_chars", 1800))
    chat_timeout = int(raw.get("chat_timeout_seconds", 180))
    work_timeout = int(raw.get("work_timeout_seconds", 3600))
    long_poll_timeout = float(raw.get("ilink_long_poll_timeout_seconds", 40.0))
    if not math.isfinite(poll_seconds) or not 0.1 <= poll_seconds <= 60:
        raise ValueError("poll_seconds 必须是 0.1 到 60 之间的有限数字")
    if max_reply_chars < 100:
        raise ValueError("max_reply_chars 不能小于 100")
    if chat_timeout < 10 or work_timeout < 10:
        raise ValueError("任务超时不能小于 10 秒")
    if not 10 <= long_poll_timeout <= 60:
        raise ValueError("ilink_long_poll_timeout_seconds 必须在 10 到 60 之间")

    response_prefix = str(raw.get("response_prefix", "[AI助手] "))
    send_received_ack = _boolean(raw, "send_received_ack", False)
    received_ack_text = str(
        raw.get("received_ack_text", "已收到，正在处理中，请稍等…")
    ).strip()
    if send_received_ack and not received_ack_text:
        raise ValueError("启用 send_received_ack 时 received_ack_text 不能为空")

    browser_channel = str(raw.get("chatgpt_browser_channel", "chrome")).strip().lower()
    supported_channels = {
        "msedge", "msedge-beta", "msedge-dev", "msedge-canary",
        "chrome", "chrome-beta", "chrome-dev", "chrome-canary",
    }
    if browser_channel not in supported_channels:
        raise ValueError("chatgpt_browser_channel 不是受支持的 Edge 或 Chrome 通道")
    proxy_value = _proxy_server(raw)
    conversation_title = str(
        raw.get("chatgpt_conversation_title", "微信助手")
    ).strip()
    if not conversation_title:
        raise ValueError("chatgpt_conversation_title 不能为空")
    if len(conversation_title) > 80:
        raise ValueError("chatgpt_conversation_title 不能超过 80 个字符")

    allowed_user_ids = _text_set(raw, "ilink_allowed_user_ids")
    codex_user_ids = _text_set(raw, "ilink_codex_user_ids")
    unavailable_codex_users = codex_user_ids - allowed_user_ids
    if unavailable_codex_users:
        users = ", ".join(sorted(unavailable_codex_users))
        raise ValueError(
            "ilink_codex_user_ids 中的用户也必须出现在 "
            f"ilink_allowed_user_ids 中：{users}"
        )

    return AppConfig(
        source=source,
        ilink_api_base_url=validate_base_url(
            str(raw.get("ilink_api_base_url", DEFAULT_API_BASE_URL))
        ),
        ilink_credentials_file=_resolve_file(
            source, raw.get("ilink_credentials_file"), ".runtime/ilink-account.json"
        ),
        ilink_state_file=_resolve_file(
            source, raw.get("ilink_state_file"), ".runtime/ilink-state.json"
        ),
        ilink_long_poll_timeout_seconds=long_poll_timeout,
        ilink_allowed_user_ids=allowed_user_ids,
        ilink_codex_user_ids=codex_user_ids,
        response_prefix=response_prefix,
        send_received_ack=send_received_ack,
        received_ack_text=received_ack_text,
        poll_seconds=poll_seconds,
        max_reply_chars=max_reply_chars,
        chatgpt_browser_channel=browser_channel,
        chatgpt_headless=_boolean(raw, "chatgpt_headless", True),
        chatgpt_proxy_server=proxy_value,
        chatgpt_conversation_title=conversation_title,
        codex_command=_required_text(raw, "codex_command")
        if "codex_command" in raw else "codex",
        chat_timeout_seconds=chat_timeout,
        work_timeout_seconds=work_timeout,
        default_project=default_project,
        projects=projects,
    )
