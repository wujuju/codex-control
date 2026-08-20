from __future__ import annotations

import re


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([^`]*)$")
_HORIZONTAL_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_CHECKBOX = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.+)$")
_BLOCKQUOTE = re.compile(r"^\s*>\s?(.*)$")
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((?:https?://|www\.)[^)\s]+\)", re.IGNORECASE)
_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_TABLE_DIVIDER_CELL = re.compile(r"^:?-{3,}:?$")


def _strip_inline_markdown(text: str) -> str:
    """Remove common Markdown decoration that WeChat does not render."""
    text = _MARKDOWN_LINK.sub(r"\1", text)
    previous = None
    while previous != text:
        previous = text
        text = _BOLD.sub(r"\2", text)
    return text


def _format_table_row(line: str) -> str | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if len(cells) < 2:
        return None
    if all(_TABLE_DIVIDER_CELL.fullmatch(cell.replace(" ", "")) for cell in cells):
        return ""
    return " ｜ ".join(_strip_inline_markdown(cell) for cell in cells)


def format_wechat_text(text: str) -> str:
    """Convert common Markdown into stable, mobile-friendly WeChat plain text.

    Fenced code bodies are deliberately left untouched so source code, shell
    snippets, Markdown examples, and ASCII layouts are not corrupted.
    """
    if not isinstance(text, str):
        return ""

    source = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not source:
        return ""

    output: list[str] = []
    in_code = False

    for raw_line in source.splitlines():
        fence = _FENCE.match(raw_line)
        if fence:
            if in_code:
                in_code = False
                if output and output[-1] != "":
                    output.append("")
            else:
                in_code = True
                language = fence.group(2).strip()
                label = f"【代码 · {language}】" if language else "【代码】"
                if output and output[-1] != "":
                    output.append("")
                output.append(label)
            continue

        if in_code:
            output.append(raw_line.rstrip())
            continue

        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            output.append("")
            continue

        heading = _HEADING.match(line)
        if heading:
            title = _strip_inline_markdown(heading.group(1)).strip()
            if title.startswith("【") and title.endswith("】"):
                output.append(title)
            else:
                output.append(f"【{title}】")
            continue

        if _HORIZONTAL_RULE.match(line):
            output.append("────────────")
            continue

        checkbox = _CHECKBOX.match(line)
        if checkbox:
            indent, checked, content = checkbox.groups()
            mark = "☑" if checked.lower() == "x" else "☐"
            output.append(f"{indent}{mark} {_strip_inline_markdown(content)}")
            continue

        bullet = _BULLET.match(line)
        if bullet:
            indent, content = bullet.groups()
            output.append(f"{indent}• {_strip_inline_markdown(content)}")
            continue

        quote = _BLOCKQUOTE.match(line)
        if quote:
            output.append(f"│ {_strip_inline_markdown(quote.group(1))}")
            continue

        table_row = _format_table_row(line)
        if table_row is not None:
            if table_row:
                output.append(table_row)
            continue

        output.append(_strip_inline_markdown(line))

    # Never send large stacks of blank lines, especially around removed table
    # separator rows or code fences.
    normalized: list[str] = []
    for line in output:
        if line == "" and (not normalized or normalized[-1] == ""):
            continue
        normalized.append(line)

    while normalized and normalized[-1] == "":
        normalized.pop()
    return "\n".join(normalized).strip()
