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
    CURRENT_CHAT = "current_chat"
    ARCHIVE_CHAT = "archive_chat"
    RENAME_CHAT = "rename_chat"
    RETRY = "retry"
    RESEND = "resend"
    CACHE_STATUS = "cache_status"
    CACHE_CLEAR = "cache_clear"
    DOCTOR = "doctor"
    CONVERSATION_LIST = "conversation_list"
    SWITCH_CHAT = "switch_chat"
    EXPORT_CHAT = "export_chat"
    SUMMARIZE_CHAT = "summarize_chat"
    RECENT_TASKS = "recent_tasks"
    PROJECT_LIST = "project_list"
    SWITCH_PROJECT = "switch_project"
    VIEW_LOGS = "view_logs"
    RECONNECT_WECHAT = "reconnect_wechat"
    RESTART_BROWSER = "restart_browser"


@dataclass(frozen=True)
class Route:
    kind: RouteKind
    prompt: str = ""
    project: str | None = None
    days: int | None = None
    number: int | None = None


_WORK = re.compile(
    r"^/干活(?:\s+(.+?))?\s*[：:]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_CONTINUE = re.compile(r"^/继续\s*[：:]\s*(.+)$", re.IGNORECASE | re.DOTALL)
_RENAME_CHAT = re.compile(
    r"^/重命名对话\s*[：:]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_CACHE_CLEAR = re.compile(
    r"^/清理缓存(?:\s*[：:]\s*(\d+)\s*天)?$",
    re.IGNORECASE,
)
_SWITCH_CHAT = re.compile(r"^/切换对话\s*[：:]\s*(\d+)$", re.IGNORECASE)
_SWITCH_PROJECT = re.compile(
    r"^/切换项目\s*[：:]\s*(.+)$",
    re.IGNORECASE,
)
_VIEW_LOGS = re.compile(r"^/查看日志(?:\s*[：:]\s*(\d+))?$", re.IGNORECASE)


def route_message(text: str) -> Route:
    cleaned = text.strip()
    if cleaned == "/状态":
        return Route(RouteKind.STATUS)
    if cleaned == "/停止":
        return Route(RouteKind.STOP)
    if cleaned in {"/帮助", "/发送帮助"}:
        return Route(RouteKind.HELP)
    if cleaned in {"/新对话", "/清理上下文"}:
        return Route(RouteKind.NEW_CHAT)
    if cleaned == "/当前对话":
        return Route(RouteKind.CURRENT_CHAT)
    if cleaned == "/对话列表":
        return Route(RouteKind.CONVERSATION_LIST)
    if cleaned == "/归档对话":
        return Route(RouteKind.ARCHIVE_CHAT)
    if cleaned == "/导出对话":
        return Route(RouteKind.EXPORT_CHAT)
    if cleaned == "/总结对话":
        return Route(RouteKind.SUMMARIZE_CHAT)
    if cleaned == "/重试":
        return Route(RouteKind.RETRY)
    if cleaned == "/重发":
        return Route(RouteKind.RESEND)
    if cleaned == "/最近任务":
        return Route(RouteKind.RECENT_TASKS)
    if cleaned == "/项目列表":
        return Route(RouteKind.PROJECT_LIST)
    if cleaned == "/缓存状态":
        return Route(RouteKind.CACHE_STATUS)
    if cleaned == "/健康检查":
        return Route(RouteKind.DOCTOR)
    if cleaned == "/重连微信":
        return Route(RouteKind.RECONNECT_WECHAT)
    if cleaned == "/重启浏览器":
        return Route(RouteKind.RESTART_BROWSER)

    match = _SWITCH_CHAT.match(cleaned)
    if match:
        return Route(RouteKind.SWITCH_CHAT, number=int(match.group(1)))

    match = _SWITCH_PROJECT.match(cleaned)
    if match:
        return Route(RouteKind.SWITCH_PROJECT, project=match.group(1).strip())

    match = _VIEW_LOGS.match(cleaned)
    if match:
        return Route(RouteKind.VIEW_LOGS, number=int(match.group(1) or 50))

    match = _RENAME_CHAT.match(cleaned)
    if match:
        return Route(RouteKind.RENAME_CHAT, prompt=match.group(1).strip())

    match = _CACHE_CLEAR.match(cleaned)
    if match:
        days = int(match.group(1) or 7)
        return Route(RouteKind.CACHE_CLEAR, days=days)

    match = _WORK.match(cleaned)
    if match:
        project = match.group(1).strip() if match.group(1) else None
        return Route(RouteKind.WORK, prompt=match.group(2).strip(), project=project)

    match = _CONTINUE.match(cleaned)
    if match:
        return Route(RouteKind.CONTINUE, prompt=match.group(1).strip())

    return Route(RouteKind.CHAT, prompt=cleaned)


def help_text(projects: list[str], default_project: str) -> str:
    project_text = "、".join(projects)
    return (
        "命令必须以 / 开头并完整匹配；其他消息作为普通聊天。\n"
        "/帮助（或 /发送帮助）：列出全部命令和说明\n"
        "/新对话（或 /清理上下文）：下条消息新建 ChatGPT 对话，旧对话保留\n"
        "/当前对话：查看当前 ChatGPT 对话信息\n"
        "/对话列表：列出最近保存的对话并编号\n"
        "/切换对话：2：切换到编号 2 的历史对话\n"
        "/归档对话：归档当前对话并开启新对话\n"
        "/重命名对话：标题：修改当前对话标题\n"
        "/导出对话：导出当前对话为 Markdown 文件\n"
        "/总结对话：总结当前上下文后开启新对话\n"
        "/重试：重新处理上一条问题\n"
        "/重发：重发最后一次成功回复，不请求 ChatGPT\n"
        f"/干活：任务：让 Codex 修改当前项目（初始为 {default_project}）\n"
        "/干活 项目名：任务：让 Codex 修改指定项目\n"
        "/继续：要求：继续最近一次 Codex 会话\n"
        "/最近任务：显示最近 Codex 任务及结果\n"
        "/项目列表：查看项目及当前选择\n"
        "/切换项目：项目名：切换当前 Codex 项目\n"
        "/状态：查看当前任务状态\n"
        "/停止：停止当前任务\n"
        "/缓存状态：查看图片缓存\n"
        "/清理缓存：7天：清理指定天数前的图片缓存\n"
        "/查看日志：50：查看最近 50 条脱敏运行日志\n"
        "/健康检查：检查微信、ChatGPT、Codex 和缓存\n"
        "/重连微信：重新连接微信 iLink（授权命令）\n"
        "/重启浏览器：重启隐藏的 ChatGPT 浏览器（授权命令）\n"
        f"可用项目：{project_text}"
    )
