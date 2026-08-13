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
    bot_name: str
    authorized_senders: frozenset[str]
    background_mode: bool
    allow_self_messages: bool
    voice_recognition: bool
    voice_retry_count: int
    response_prefix: str
    send_ready_message: bool
    poll_seconds: float
    max_reply_chars: int
    chat_model: str
    chat_reasoning_effort: str
    chat_max_output_tokens: int
    codex_command: str
    chat_timeout_seconds: int
    work_timeout_seconds: int
    default_project: str
    projects: dict[str, Path]

    @property
    def runtime_dir(self) -> Path:
        return self.source.parent / ".runtime"


def _required_text(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"配置项 {key!r} 不能为空")
    return value


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

    poll_seconds = float(raw.get("poll_seconds", 1.0))
    max_reply_chars = int(raw.get("max_reply_chars", 1800))
    chat_timeout = int(raw.get("chat_timeout_seconds", 180))
    chat_max_output_tokens = int(raw.get("chat_max_output_tokens", 1200))
    work_timeout = int(raw.get("work_timeout_seconds", 3600))
    voice_retry_count = int(raw.get("voice_retry_count", 3))
    if poll_seconds < 0.2:
        raise ValueError("poll_seconds 不能小于 0.2")
    if max_reply_chars < 100:
        raise ValueError("max_reply_chars 不能小于 100")
    if chat_timeout < 10 or work_timeout < 10:
        raise ValueError("任务超时不能小于 10 秒")
    if chat_max_output_tokens < 100:
        raise ValueError("chat_max_output_tokens 不能小于 100")
    if not 1 <= voice_retry_count <= 5:
        raise ValueError("voice_retry_count 必须在 1 到 5 之间")

    response_prefix = str(raw.get("response_prefix", "[Codex助手] "))
    if not response_prefix:
        raise ValueError("response_prefix 不能为空，否则可能形成自动回复循环")

    chat_type = str(raw.get("chat_type", "friend")).strip().lower()
    if chat_type not in {"friend", "group", "auto"}:
        raise ValueError("chat_type 必须是 friend、group 或 auto")

    bot_name = str(raw.get("bot_name", "ChatGpt机器人")).strip()
    if not bot_name:
        raise ValueError("bot_name 不能为空")

    chat_model = str(raw.get("chat_model", "gpt-5.6-terra")).strip()
    if not chat_model:
        raise ValueError("chat_model 不能为空")
    chat_reasoning_effort = str(raw.get("chat_reasoning_effort", "low")).strip().lower()
    if chat_reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError(
            "chat_reasoning_effort 必须是 none、low、medium、high、xhigh 或 max"
        )

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
        contact=_required_text(raw, "contact"),
        chat_type=chat_type,
        bot_name=bot_name,
        authorized_senders=authorized_senders,
        background_mode=bool(raw.get("background_mode", True)),
        allow_self_messages=bool(raw.get("allow_self_messages", True)),
        voice_recognition=bool(raw.get("voice_recognition", True)),
        voice_retry_count=voice_retry_count,
        response_prefix=response_prefix,
        send_ready_message=bool(raw.get("send_ready_message", True)),
        poll_seconds=poll_seconds,
        max_reply_chars=max_reply_chars,
        chat_model=chat_model,
        chat_reasoning_effort=chat_reasoning_effort,
        chat_max_output_tokens=chat_max_output_tokens,
        codex_command=_required_text(raw, "codex_command")
        if "codex_command" in raw
        else "codex",
        chat_timeout_seconds=chat_timeout,
        work_timeout_seconds=work_timeout,
        default_project=default_project,
        projects=projects,
    )
