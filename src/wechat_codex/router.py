from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class RouteKind(str, Enum):
    CHAT = "chat"
    WORK = "work"
    CONTINUE = "continue"
    STATUS = "status"
    STOP = "stop"
    HELP = "help"
    NEW_CHAT = "new_chat"


@dataclass(frozen=True)
class Route:
    kind: RouteKind
    prompt: str = ""
    project: str | None = None


_WORK = re.compile(
    r"^(?:干活|/work)(?:\s+([A-Za-z0-9_.-]+))?\s*[：:]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_CONTINUE = re.compile(
    r"^(?:继续|/continue)\s*[：:]\s*(.+)$", re.IGNORECASE | re.DOTALL
)


def route_message(text: str) -> Route:
    cleaned = text.strip()
    lowered = cleaned.lower()
    if lowered in {"状态", "/status"}:
        return Route(RouteKind.STATUS)
    if lowered in {"停止", "/stop", "取消"}:
        return Route(RouteKind.STOP)
    if lowered in {"帮助", "/help", "?", "？"}:
        return Route(RouteKind.HELP)
    if lowered in {"新对话", "/new"}:
        return Route(RouteKind.NEW_CHAT)

    match = _WORK.match(cleaned)
    if match:
        return Route(
            RouteKind.WORK, prompt=match.group(2).strip(), project=match.group(1)
        )

    match = _CONTINUE.match(cleaned)
    if match:
        return Route(RouteKind.CONTINUE, prompt=match.group(1).strip())

    return Route(RouteKind.CHAT, prompt=cleaned)


def help_text(projects: list[str], default_project: str) -> str:
    project_text = "、".join(projects)
    return (
        "直接发消息：普通聊天\n"
        f"干活：任务：让 Codex 修改默认项目 {default_project}\n"
        "干活 项目名：任务：修改指定项目\n"
        "继续：要求：继续上次干活会话\n"
        "新对话：下条消息新建 ChatGPT 对话\n"
        "状态：查看任务\n"
        "停止：终止当前任务\n"
        f"可用项目：{project_text}"
    )
