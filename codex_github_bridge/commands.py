from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CommandKind(str, Enum):
    HELP = "help"
    STATUS = "status"
    SUMMARY = "summary"
    DIFF = "diff"
    TEST = "test"
    STOP = "stop"
    CONTINUE = "continue"
    NEW = "new"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    kind: CommandKind
    prompt: str = ""
    raw: str = ""


def _strip_prefix(text: str, prefix: str) -> str | None:
    body = text.strip()
    if not prefix:
        return body
    if body.startswith(prefix):
        return body[len(prefix) :].strip()
    return None


def parse_command(text: str, prefix: str = "") -> ParsedCommand | None:
    body = _strip_prefix(text, prefix)
    if body is None:
        return None
    body = body.strip()
    if not body:
        return None

    normalized = body.lower()
    if normalized in {"帮助", "help", "/help", "?", "？"}:
        return ParsedCommand(CommandKind.HELP, raw=body)
    if normalized in {"状态", "status", "/status"}:
        return ParsedCommand(CommandKind.STATUS, raw=body)
    if normalized in {"总结", "summary", "last", "最近"}:
        return ParsedCommand(CommandKind.SUMMARY, raw=body)
    if normalized in {"diff", "差异", "改动", "变更"}:
        return ParsedCommand(CommandKind.DIFF, raw=body)
    if normalized in {"测试", "test", "run test", "run tests"}:
        return ParsedCommand(CommandKind.TEST, raw=body)
    if normalized in {"停止", "stop", "cancel", "取消"}:
        return ParsedCommand(CommandKind.STOP, raw=body)

    starters = [
        ("继续", CommandKind.CONTINUE),
        ("continue", CommandKind.CONTINUE),
        ("resume", CommandKind.CONTINUE),
        ("新任务", CommandKind.NEW),
        ("new", CommandKind.NEW),
        ("start", CommandKind.NEW),
    ]
    for starter, kind in starters:
        if normalized == starter:
            return ParsedCommand(kind, prompt="", raw=body)
        if normalized.startswith(starter + " ") or normalized.startswith(starter + "\n"):
            prompt = body[len(starter) :].strip()
            return ParsedCommand(kind, prompt=prompt, raw=body)

    return None


def help_text(prefix: str = "") -> str:
    p = prefix or ""
    return (
        "可用命令：\n\n"
        f"- `{p}状态`：查看 bridge / Codex 状态\n"
        f"- `{p}继续 <prompt>`：继续最近一次 Codex exec session\n"
        f"- `{p}新任务 <prompt>`：启动新的 Codex exec 任务\n"
        f"- `{p}总结`：查看上次结果摘要\n"
        f"- `{p}diff`：查看 git status 和 diff stat\n"
        f"- `{p}测试`：运行本地 `.env` 里的 TEST_COMMAND\n"
        f"- `{p}停止`：终止当前 Codex 进程\n\n"
        "安全限制：GitHub 评论不能直接执行 shell，只能走以上白名单命令。"
    )
