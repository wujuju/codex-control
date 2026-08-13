from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AppConfig:
    source: Path
    wecom_bot_id: str
    wecom_bot_secret_env: str
    wecom_websocket_url: str
    group_only: bool
    allowed_group_chat_ids: frozenset[str]
    authorized_senders: frozenset[str]
    default_group_chat_name: str
    group_chat_names: dict[str, str]
    user_chat_names: dict[str, str]
    chatgpt_browser_channel: str
    chatgpt_headless: bool
    chat_timeout_seconds: int
    wecom_timeout_seconds: int
    response_prefix: str
    max_reply_bytes: int
    codex_command: str
    work_timeout_seconds: int
    default_project: str
    projects: dict[str, Path]

    @property
    def runtime_dir(self) -> Path:
        return self.source.parent / ".runtime"

    @property
    def chatgpt_profile_dir(self) -> Path:
        return self.runtime_dir / "chatgpt-plus-profile"

    def require_secret(self, environment_name: str) -> str:
        value = os.environ.get(environment_name, "").strip()
        if not value:
            raise RuntimeError(f"环境变量 {environment_name} 尚未设置")
        return value

    @property
    def wecom_bot_secret(self) -> str:
        return self.require_secret(self.wecom_bot_secret_env)


def _required_text(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"配置项 {key!r} 不能为空")
    return value


def _environment_name(data: dict[str, Any], key: str, default: str) -> str:
    value = str(data.get(key, default)).strip()
    if not value or not value.replace("_", "A").isalnum():
        raise ValueError(f"配置项 {key!r} 必须是环境变量名称")
    return value


def _text_set(data: dict[str, Any], key: str) -> frozenset[str]:
    values = data.get(key) or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise ValueError(f"配置项 {key!r} 必须是字符串列表")
    return frozenset(str(value).strip() for value in values if str(value).strip())


def _text_map(data: dict[str, Any], key: str) -> dict[str, str]:
    values = data.get(key) or {}
    if not isinstance(values, dict):
        raise ValueError(f"配置项 {key!r} 必须是键值对象")
    result: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        name = str(raw_key).strip()
        title = str(raw_value).strip()
        if not name or not title:
            raise ValueError(f"配置项 {key!r} 不能包含空的 ID 或名称")
        result[name] = title
    return result


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

    wecom_timeout = int(raw.get("wecom_timeout_seconds", 30))
    chat_timeout = int(raw.get("chat_timeout_seconds", 180))
    work_timeout = int(raw.get("work_timeout_seconds", 3600))
    max_reply_bytes = int(raw.get("max_reply_bytes", 18000))
    if wecom_timeout < 10 or chat_timeout < 10 or work_timeout < 10:
        raise ValueError("超时时间不能小于 10 秒")
    if not 256 <= max_reply_bytes <= 20480:
        raise ValueError("max_reply_bytes 必须在 256 到 20480 之间")

    browser_channel = str(raw.get("chatgpt_browser_channel", "chrome")).lower()
    if browser_channel not in {
        "chrome",
        "chrome-beta",
        "chrome-dev",
        "chrome-canary",
        "msedge",
        "msedge-beta",
        "msedge-dev",
        "msedge-canary",
    }:
        raise ValueError("chatgpt_browser_channel 必须是受支持的 Chrome 或 Edge 通道")

    websocket_url = str(
        raw.get("wecom_websocket_url", "wss://openws.work.weixin.qq.com")
    ).strip()
    if not websocket_url.startswith("wss://"):
        raise ValueError("wecom_websocket_url 必须使用 wss://")

    authorized_senders = _text_set(raw, "authorized_senders")
    if not authorized_senders:
        raise ValueError("authorized_senders 至少需要一个企业微信 userid")

    return AppConfig(
        source=source,
        wecom_bot_id=_required_text(raw, "wecom_bot_id"),
        wecom_bot_secret_env=_environment_name(
            raw, "wecom_bot_secret_env", "WECOM_BOT_SECRET"
        ),
        wecom_websocket_url=websocket_url,
        group_only=bool(raw.get("group_only", True)),
        allowed_group_chat_ids=_text_set(raw, "allowed_group_chat_ids"),
        authorized_senders=authorized_senders,
        default_group_chat_name=str(raw.get("default_group_chat_name", "")).strip(),
        group_chat_names=_text_map(raw, "group_chat_names"),
        user_chat_names=_text_map(raw, "user_chat_names"),
        chatgpt_browser_channel=browser_channel,
        chatgpt_headless=bool(raw.get("chatgpt_headless", True)),
        chat_timeout_seconds=chat_timeout,
        wecom_timeout_seconds=wecom_timeout,
        response_prefix=str(raw.get("response_prefix", "")).strip(),
        max_reply_bytes=max_reply_bytes,
        codex_command=str(raw.get("codex_command", "codex")).strip() or "codex",
        work_timeout_seconds=work_timeout,
        default_project=default_project,
        projects=projects,
    )
