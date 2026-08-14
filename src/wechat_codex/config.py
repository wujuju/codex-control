from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AppConfig:
    source: Path
    contact: str
    chat_type: str
    wechat_message_source: str
    wx_cli_path: str | None
    wx_cli_username: str | None
    wx_cli_contacts: tuple[str, ...]
    wx_cli_groups: tuple[str, ...]
    wx_cli_timeout_seconds: float
    bot_name: str
    authorized_senders: frozenset[str]
    background_mode: bool
    allow_self_messages: bool
    voice_recognition: bool
    voice_retry_count: int
    response_prefix: str
    send_ready_message: bool
    send_received_ack: bool
    received_ack_text: str
    poll_seconds: float
    max_reply_chars: int
    chatgpt_browser_channel: str
    chatgpt_headless: bool
    chatgpt_proxy_server: str | None
    chatgpt_conversation_title_prefix: str
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


def _text_list(data: dict[str, Any], key: str) -> tuple[str, ...] | None:
    if key not in data:
        return None
    values = data[key]
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise ValueError(f"配置项 {key!r} 必须是名称列表")
    result = tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    return result


def load_config(path: str | Path) -> AppConfig:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"配置文件不存在：{source}")

    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件顶层必须是 YAML 对象")

    contact = _required_text(raw, "contact")

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

    poll_seconds = float(raw.get("poll_seconds", 1.0))
    max_reply_chars = int(raw.get("max_reply_chars", 1800))
    chat_timeout = int(raw.get("chat_timeout_seconds", 180))
    work_timeout = int(raw.get("work_timeout_seconds", 3600))
    voice_retry_count = int(raw.get("voice_retry_count", 3))
    if poll_seconds < 0.2:
        raise ValueError("poll_seconds 不能小于 0.2")
    if max_reply_chars < 100:
        raise ValueError("max_reply_chars 不能小于 100")
    if chat_timeout < 10 or work_timeout < 10:
        raise ValueError("任务超时不能小于 10 秒")
    if not 1 <= voice_retry_count <= 5:
        raise ValueError("voice_retry_count 必须在 1 到 5 之间")

    response_prefix = str(raw.get("response_prefix", "[Codex助手] "))
    if not response_prefix:
        raise ValueError("response_prefix 不能为空，否则可能形成自动回复循环")
    send_received_ack = bool(raw.get("send_received_ack", False))
    received_ack_text = str(
        raw.get("received_ack_text", "已收到，正在处理中，请稍等…")
    ).strip()
    if send_received_ack and not received_ack_text:
        raise ValueError("启用 send_received_ack 时 received_ack_text 不能为空")

    chat_type = str(raw.get("chat_type", "friend")).strip().lower()
    if chat_type not in {"friend", "group", "auto"}:
        raise ValueError("chat_type 必须是 friend、group 或 auto")

    wechat_message_source = str(
        raw.get("wechat_message_source", "uia")
    ).strip().lower().replace("-", "_")
    if wechat_message_source not in {"uia", "wx_cli"}:
        raise ValueError("wechat_message_source 必须是 uia 或 wx_cli")
    wx_cli_path = str(raw.get("wx_cli_path", "")).strip() or None
    wx_cli_username = str(raw.get("wx_cli_username", "")).strip() or None
    configured_contacts = _text_list(raw, "wx_cli_contacts")
    configured_groups = _text_list(raw, "wx_cli_groups")
    wx_cli_contacts = configured_contacts
    wx_cli_groups = configured_groups
    if wx_cli_contacts is None:
        wx_cli_contacts = (contact,) if chat_type in {"friend", "auto"} else ()
    if wx_cli_groups is None:
        wx_cli_groups = (contact,) if chat_type in {"group", "auto"} else ()
    if wechat_message_source == "wx_cli" and not (wx_cli_contacts or wx_cli_groups):
        raise ValueError("wx_cli_contacts 和 wx_cli_groups 至少需要配置一个会话")
    wx_cli_timeout_seconds = float(raw.get("wx_cli_timeout_seconds", 30.0))
    if wx_cli_timeout_seconds < 1:
        raise ValueError("wx_cli_timeout_seconds 不能小于 1")

    bot_name = str(raw.get("bot_name", "ChatGpt机器人")).strip()
    if not bot_name:
        raise ValueError("bot_name 不能为空")

    chatgpt_browser_channel = str(
        raw.get("chatgpt_browser_channel", "msedge")
    ).strip().lower()
    if chatgpt_browser_channel not in {
        "msedge",
        "msedge-beta",
        "msedge-dev",
        "msedge-canary",
        "chrome",
        "chrome-beta",
        "chrome-dev",
        "chrome-canary",
    }:
        raise ValueError("chatgpt_browser_channel 必须是受支持的 Edge 或 Chrome 通道")

    proxy_value = str(raw.get("chatgpt_proxy_server", "")).strip()
    if proxy_value and "://" not in proxy_value:
        proxy_value = f"http://{proxy_value}"
    chatgpt_proxy_server = proxy_value or None
    chatgpt_conversation_title_prefix = str(
        raw.get("chatgpt_conversation_title_prefix", "微信")
    ).strip()
    if not chatgpt_conversation_title_prefix:
        raise ValueError("chatgpt_conversation_title_prefix 不能为空")

    authorized_values = raw.get("authorized_senders", ["無惧"])
    if isinstance(authorized_values, str):
        authorized_values = [authorized_values]
    if not isinstance(authorized_values, list):
        raise ValueError("authorized_senders 必须是名称列表")
    authorized_senders = frozenset(
        str(value).strip() for value in authorized_values if str(value).strip()
    )
    if not authorized_senders:
        raise ValueError("authorized_senders 至少需要一个名称")

    return AppConfig(
        source=source,
        contact=contact,
        chat_type=chat_type,
        wechat_message_source=wechat_message_source,
        wx_cli_path=wx_cli_path,
        wx_cli_username=wx_cli_username,
        wx_cli_contacts=wx_cli_contacts,
        wx_cli_groups=wx_cli_groups,
        wx_cli_timeout_seconds=wx_cli_timeout_seconds,
        bot_name=bot_name,
        authorized_senders=authorized_senders,
        background_mode=bool(raw.get("background_mode", True)),
        allow_self_messages=bool(raw.get("allow_self_messages", True)),
        voice_recognition=bool(raw.get("voice_recognition", True)),
        voice_retry_count=voice_retry_count,
        response_prefix=response_prefix,
        send_ready_message=bool(raw.get("send_ready_message", True)),
        send_received_ack=send_received_ack,
        received_ack_text=received_ack_text,
        poll_seconds=poll_seconds,
        max_reply_chars=max_reply_chars,
        chatgpt_browser_channel=chatgpt_browser_channel,
        chatgpt_headless=bool(raw.get("chatgpt_headless", True)),
        chatgpt_proxy_server=chatgpt_proxy_server,
        chatgpt_conversation_title_prefix=chatgpt_conversation_title_prefix,
        codex_command=_required_text(raw, "codex_command")
        if "codex_command" in raw
        else "codex",
        chat_timeout_seconds=chat_timeout,
        work_timeout_seconds=work_timeout,
        default_project=default_project,
        projects=projects,
    )
