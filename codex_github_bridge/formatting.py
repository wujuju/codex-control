from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

BRIDGE_MARKER = "<!-- codex-github-bridge -->"
MAX_COMMENT_CHARS = 58000

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def tail_text(text: str, max_chars: int = 6000) -> str:
    text = strip_ansi(text or "")
    if len(text) <= max_chars:
        return text
    return "... output truncated ...\n" + text[-max_chars:]


def fence(text: str, language: str = "") -> str:
    text = text.replace("```", "`\u200b``")
    return f"```{language}\n{text}\n```"


def limit_comment(body: str) -> str:
    if len(body) <= MAX_COMMENT_CHARS:
        return body
    return body[: MAX_COMMENT_CHARS - 2000] + "\n\n... comment truncated ...\n\n" + body[-1500:]


def bridge_comment(body: str) -> str:
    return limit_comment(f"{BRIDGE_MARKER}\n{body}")


def read_tail(path: str | Path, max_chars: int = 6000) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    data = p.read_text(encoding="utf-8", errors="replace")
    return tail_text(data, max_chars=max_chars)
