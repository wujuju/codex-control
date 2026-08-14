from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .ilink_api import DEFAULT_API_BASE_URL, validate_base_url


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

    @property
    def runtime_dir(self) -> Path:
        return self.source.parent / ".runtime"

    @property
    def chatgpt_profile_dir(self) -> Path:
        return self.runtime_dir / "chatgpt-plus-profile"


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
    return frozenset(str(value).strip() for value in values if str(value).strip())


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

    poll_seconds = float(raw.get("poll_seconds", 0.5))
    max_reply_chars = int(raw.get("max_reply_chars", 1800))
    chat_timeout = int(raw.get("chat_timeout_seconds", 180))
    work_timeout = int(raw.get("work_timeout_seconds", 3600))
    long_poll_timeout = float(raw.get("ilink_long_poll_timeout_seconds", 40.0))
    if poll_seconds < 0.1:
        raise ValueError("poll_seconds 不能小于 0.1")
    if max_reply_chars < 100:
        raise ValueError("max_reply_chars 不能小于 100")
    if chat_timeout < 10 or work_timeout < 10:
        raise ValueError("任务超时不能小于 10 秒")
    if not 10 <= long_poll_timeout <= 60:
        raise ValueError("ilink_long_poll_timeout_seconds 必须在 10 到 60 之间")

    response_prefix = str(raw.get("response_prefix", "[Codex助手] "))
    send_received_ack = bool(raw.get("send_received_ack", True))
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
    proxy_value = str(raw.get("chatgpt_proxy_server", "")).strip()
    if proxy_value and "://" not in proxy_value:
        proxy_value = f"http://{proxy_value}"
    conversation_title = str(
        raw.get("chatgpt_conversation_title", "微信助手")
    ).strip()
    if not conversation_title:
        raise ValueError("chatgpt_conversation_title 不能为空")
    if len(conversation_title) > 80:
        raise ValueError("chatgpt_conversation_title 不能超过 80 个字符")

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
        ilink_allowed_user_ids=_text_set(raw, "ilink_allowed_user_ids"),
        ilink_codex_user_ids=_text_set(raw, "ilink_codex_user_ids"),
        response_prefix=response_prefix,
        send_received_ack=send_received_ack,
        received_ack_text=received_ack_text,
        poll_seconds=poll_seconds,
        max_reply_chars=max_reply_chars,
        chatgpt_browser_channel=browser_channel,
        chatgpt_headless=bool(raw.get("chatgpt_headless", False)),
        chatgpt_proxy_server=proxy_value or None,
        chatgpt_conversation_title=conversation_title,
        codex_command=_required_text(raw, "codex_command")
        if "codex_command" in raw else "codex",
        chat_timeout_seconds=chat_timeout,
        work_timeout_seconds=work_timeout,
        default_project=default_project,
        projects=projects,
    )
