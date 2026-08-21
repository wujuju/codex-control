from __future__ import annotations

import re


_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$")
_FENCE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
_HORIZONTAL_RULE = re.compile(
    r"^ {0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$"
)
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_ORDERED_LIST = re.compile(r"^(\s*)(\d+)[.)]\s+(.+)$")
_CHECKBOX = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.+)$")
_BLOCKQUOTE = re.compile(r"^(\s*)(>+)\s?(.*)$")
_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_STRIKETHROUGH = re.compile(r"~~(.+?)~~")
_ITALIC_STAR = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_ITALIC_UNDERSCORE = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_INLINE_CODE = re.compile(r"(`+)([^\n]+?)\1")
_MARKDOWN_IMAGE = re.compile(
    r"!\[([^\]\n]*)\]\(((?:https?://|www\.)[^\s)]+)\)", re.IGNORECASE
)
_MARKDOWN_LINK = re.compile(
    r"\[([^\]\n]+)\]\(((?:https?://|www\.)[^\s)]+)\)", re.IGNORECASE
)
_AUTOLINK = re.compile(r"<((?:https?://|www\.)[^<>\s]+)>", re.IGNORECASE)
_BARE_URL = re.compile(r"(?<![\w@])(?:https?://|www\.)[^\s<>\u3000]+", re.IGNORECASE)
_MARKDOWN_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_TABLE_DIVIDER_CELL = re.compile(r"^:?-{3,}:?$")


def _remove_emphasis(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _BOLD.sub(r"\2", text)
        text = _STRIKETHROUGH.sub(r"\1", text)
    text = _ITALIC_STAR.sub(r"\1", text)
    return _ITALIC_UNDERSCORE.sub(r"\1", text)


def _strip_inline_markdown(text: str) -> str:
    """Render common inline Markdown as compact WeChat-friendly plain text."""
    protected: list[str] = []
    placeholder_start = "\ue000"
    while placeholder_start in text:
        placeholder_start += "\ue000"
    placeholder_pattern = re.compile(
        rf"{re.escape(placeholder_start)}(\d+)\ue001"
    )

    def hold(value: str) -> str:
        protected.append(value)
        return f"{placeholder_start}{len(protected) - 1}\ue001"

    def render_code(match: re.Match[str]) -> str:
        content = match.group(2)
        if len(content) > 1 and content.startswith(" ") and content.endswith(" "):
            content = content[1:-1]
        return hold(f"「{content}」")

    def render_image(match: re.Match[str]) -> str:
        alt = _remove_emphasis(match.group(1)).strip()
        url = match.group(2)
        label = f"图片：{alt}" if alt else "图片"
        return hold(f"{label}（{url}）")

    def render_link(match: re.Match[str]) -> str:
        label = _remove_emphasis(match.group(1)).strip()
        url = match.group(2)
        return hold(url if label == url else f"{label}：{url}")

    def render_bare_url(match: re.Match[str]) -> str:
        url = match.group(0)
        suffix = ""
        while url and url[-1] in ".,;:!?，。；：！？、":
            suffix = url[-1] + suffix
            url = url[:-1]
        return hold(url) + suffix

    text = _INLINE_CODE.sub(render_code, text)
    text = _MARKDOWN_IMAGE.sub(render_image, text)
    text = _MARKDOWN_LINK.sub(render_link, text)
    text = _AUTOLINK.sub(lambda match: hold(match.group(1)), text)
    text = _BARE_URL.sub(render_bare_url, text)
    text = _remove_emphasis(text)
    text = _MARKDOWN_ESCAPE.sub(r"\1", text)

    return placeholder_pattern.sub(lambda match: protected[int(match.group(1))], text)


def _is_escaped(text: str, position: int) -> bool:
    slashes = 0
    position -= 1
    while position >= 0 and text[position] == "\\":
        slashes += 1
        position -= 1
    return slashes % 2 == 1


def _split_table_row(line: str) -> list[str] | None:
    """Split a Markdown table row without breaking escaped pipes or code spans."""
    row = line.strip()
    if "|" not in row:
        return None
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not _is_escaped(row, len(row) - 1):
        row = row[:-1]

    cells: list[str] = []
    current: list[str] = []
    code_ticks = 0
    separators = 0
    position = 0
    while position < len(row):
        char = row[position]
        if char == "\\" and position + 1 < len(row):
            current.extend((char, row[position + 1]))
            position += 2
            continue
        if char == "`":
            end = position + 1
            while end < len(row) and row[end] == "`":
                end += 1
            tick_count = end - position
            if code_ticks == 0:
                code_ticks = tick_count
            elif code_ticks == tick_count:
                code_ticks = 0
            current.append(row[position:end])
            position = end
            continue
        if char == "|" and code_ticks == 0:
            cells.append("".join(current).strip())
            current = []
            separators += 1
        else:
            current.append(char)
        position += 1

    cells.append("".join(current).strip())
    return cells if separators and len(cells) >= 2 else None


def _is_table_divider(cells: list[str] | None, column_count: int) -> bool:
    if cells is None or len(cells) != column_count:
        return False
    return all(
        _TABLE_DIVIDER_CELL.fullmatch(cell.replace(" ", "")) for cell in cells
    )


def _format_mobile_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    headers = [_strip_inline_markdown(header).strip() for header in headers]
    if not rows:
        return [" / ".join(header for header in headers if header)]

    rendered: list[str] = []
    multiple_rows = len(rows) > 1
    for row_index, row in enumerate(rows, start=1):
        values = [_strip_inline_markdown(cell).strip() for cell in row]
        column_count = max(len(headers), len(values))
        row_headers = headers + [""] * (column_count - len(headers))
        values.extend([""] * (column_count - len(values)))

        first_value = values[0] or row_headers[0] or f"第 {row_index} 项"
        marker = f"{row_index}." if multiple_rows else "•"
        rendered.append(f"{marker} {first_value}")
        for column_index, value in enumerate(values[1:], start=1):
            header = row_headers[column_index] or f"第 {column_index + 1} 列"
            rendered.append(f"  {header}：{value or '—'}")
        if row_index < len(rows):
            rendered.append("")
    return rendered


def _mobile_indent(indent: str) -> str:
    width = len(indent.expandtabs(4))
    return "  " * min(width // 2, 2)


def _code_language(info: str) -> str:
    if not info.strip():
        return ""
    return info.strip().split(maxsplit=1)[0].strip("{}.")[:24]


def format_wechat_text(text: str) -> str:
    """Convert Markdown into compact plain text for narrow WeChat screens.

    Prose is left to the WeChat client to wrap naturally. Wide tables become
    vertical cards, while fenced code keeps its original whitespace.
    """
    if not isinstance(text, str):
        return ""

    source = text.replace("\r\n", "\n").replace("\r", "\n")
    if not source.strip():
        return ""

    # The boolean marks verbatim code lines. It lets final whitespace cleanup
    # remove layout-only blank lines without altering a code block's body.
    output: list[tuple[str, bool]] = []

    def append_blank() -> None:
        if output and output[-1][0] != "":
            output.append(("", False))

    def append_line(line: str, *, verbatim: bool = False) -> None:
        output.append((line, verbatim))

    lines = source.split("\n")
    fence_char = ""
    fence_length = 0
    position = 0

    while position < len(lines):
        raw_line = lines[position]
        fence = _FENCE.match(raw_line)

        if fence_char:
            if (
                fence
                and fence.group("marker")[0] == fence_char
                and len(fence.group("marker")) >= fence_length
                and not fence.group("info").strip()
            ):
                fence_char = ""
                fence_length = 0
                append_blank()
            else:
                append_line(raw_line, verbatim=True)
            position += 1
            continue

        if fence and not (
            fence.group("marker").startswith("`") and "`" in fence.group("info")
        ):
            marker = fence.group("marker")
            fence_char = marker[0]
            fence_length = len(marker)
            language = _code_language(fence.group("info"))
            append_blank()
            append_line(f"【代码 · {language}】" if language else "【代码】")
            position += 1
            continue

        line = raw_line.rstrip()
        if not line.strip():
            append_blank()
            position += 1
            continue

        # A table is recognized only when a header is immediately followed by
        # a valid divider, avoiding accidental conversion of ordinary pipes.
        if position + 1 < len(lines):
            headers = _split_table_row(line)
            divider = _split_table_row(lines[position + 1])
            if headers is not None and _is_table_divider(divider, len(headers)):
                rows: list[list[str]] = []
                cursor = position + 2
                while cursor < len(lines):
                    row = _split_table_row(lines[cursor])
                    if row is None:
                        break
                    rows.append(row)
                    cursor += 1
                append_blank()
                for rendered_line in _format_mobile_table(headers, rows):
                    if rendered_line:
                        append_line(rendered_line)
                    else:
                        append_blank()
                append_blank()
                position = cursor
                continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading.group(2)).strip()
            title = _strip_inline_markdown(title)
            if title:
                append_blank()
                if level == 1:
                    append_line(
                        title
                        if title.startswith("【") and title.endswith("】")
                        else f"【{title}】"
                    )
                else:
                    append_line(title if title.startswith("▌") else f"▌{title}")
                append_blank()
            position += 1
            continue

        if _HORIZONTAL_RULE.match(line):
            append_blank()
            append_line("────────────")
            append_blank()
            position += 1
            continue

        checkbox = _CHECKBOX.match(line)
        if checkbox:
            indent, checked, content = checkbox.groups()
            mark = "☑" if checked.lower() == "x" else "☐"
            append_line(
                f"{_mobile_indent(indent)}{mark} {_strip_inline_markdown(content)}"
            )
            position += 1
            continue

        bullet = _BULLET.match(line)
        if bullet:
            indent, content = bullet.groups()
            append_line(
                f"{_mobile_indent(indent)}• {_strip_inline_markdown(content)}"
            )
            position += 1
            continue

        ordered = _ORDERED_LIST.match(line)
        if ordered:
            indent, number, content = ordered.groups()
            append_line(
                f"{_mobile_indent(indent)}{number}. {_strip_inline_markdown(content)}"
            )
            position += 1
            continue

        quote = _BLOCKQUOTE.match(line)
        if quote:
            _indent, markers, content = quote.groups()
            quote_indent = "  " * min(len(markers) - 1, 2)
            rendered_content = _strip_inline_markdown(content)
            append_line(f"{quote_indent}│ {rendered_content}".rstrip())
            position += 1
            continue

        append_line(_strip_inline_markdown(line))
        position += 1

    while output and output[0][0] == "" and not output[0][1]:
        output.pop(0)
    while output and output[-1][0] == "" and not output[-1][1]:
        output.pop()
    return "\n".join(line for line, _verbatim in output)
