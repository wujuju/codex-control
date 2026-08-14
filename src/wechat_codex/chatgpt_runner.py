from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


log = logging.getLogger(__name__)


CHATGPT_HOME = "https://chatgpt.com/"
CHAT_URL = re.compile(r"^https://chatgpt\.com/(?:g/[^/]+/)?c/", re.IGNORECASE)
PROMPT_SELECTOR = '[data-testid="prompt-textarea"], #prompt-textarea'
ACCOUNT_SELECTOR = '[data-testid="accounts-profile-button"]'
LOGIN_SELECTOR = '[data-testid="login-button"]'
SEND_SELECTOR = (
    'button[data-testid="send-button"], '
    'button[data-testid="composer-submit-button"]'
)
STOP_SELECTOR = (
    'button[data-testid="stop-button"], '
    'button[data-testid="composer-stop-button"]'
)
FILE_INPUT_SELECTOR = 'input[type="file"]'
ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
ASSISTANT_FALLBACK_SELECTOR = 'article[data-turn="assistant"]'
GENERATED_IMAGE_SELECTOR = (
    'main img[alt^="Generated image" i], '
    'main img[alt*="生成"], '
    'main img[src*="/backend-api/estuary/content"], '
    'main img[src*="oaiusercontent.com"], '
    'main [data-testid*="generated-image" i] img, '
    'main [data-testid*="image-generation" i] img'
)
MAX_REPLY_IMAGE_BYTES = 20 * 1024 * 1024
MIN_REPLY_IMAGE_NATURAL_SIZE = 128
MIN_REPLY_IMAGE_DISPLAY_SIZE = 64
IMAGE_METADATA_SCRIPT = """img => {
    const box = img.getBoundingClientRect();
    const style = getComputedStyle(img);
    const source = img.currentSrc || img.src || '';
    const alt = (img.getAttribute('alt') || '').trim();
    const turn = img.closest('[data-testid^="conversation-turn-"]');
    const turnId = turn ? (turn.getAttribute('data-testid') || '') : '';
    const userTurn = Boolean(
        turn && turn.querySelector('[data-message-author-role="user"]')
    );
    let mediaId = '';
    let mediaKey = '';
    let generated = Boolean(
        /^Generated image/i.test(alt) || alt.includes('生成') ||
        img.closest(
            '[data-testid*="generated-image" i], '
            + '[data-testid*="image-generation" i]'
        )
    );
    try {
        const parsed = new URL(source, location.href);
        mediaId = parsed.searchParams.get('id') || '';
        mediaKey = mediaId
            ? `file:${mediaId}`
            : `url:${parsed.hostname}${parsed.pathname}`;
        const hostname = parsed.hostname.toLowerCase();
        generated = generated ||
            hostname === 'oaiusercontent.com' ||
            hostname.endsWith('.oaiusercontent.com') ||
            parsed.pathname.includes('/backend-api/estuary/content');
    } catch (_) {}
    return {
        source,
        alt,
        mediaId,
        mediaKey: mediaKey || (source ? `source:${source.slice(0, 256)}` : ''),
        turnId,
        userTurn,
        generated,
        naturalWidth: img.naturalWidth,
        naturalHeight: img.naturalHeight,
        renderedWidth: box.width,
        renderedHeight: box.height,
        usable: Boolean(
            source && img.naturalWidth >= 128 && img.naturalHeight >= 128 &&
            box.width >= 64 && box.height >= 64 && generated &&
            style.visibility !== 'hidden' &&
            style.display !== 'none'
        )
    };
}"""
REPLY_TEXT_SCRIPT = """node => {
    const clone = node.cloneNode(true);
    const citationSelectors = [
        '[data-testid="webpage-citation-pill"]',
        '[data-testid^="webpage-citation-"]',
        '[data-testid*="citation-card" i]',
        '[data-testid*="sources-footer" i]',
        '[data-testid*="source-footer" i]',
        '[aria-label^="Citation" i]',
        '[aria-label*="citation" i]',
        '[aria-label^="引用"]',
        'sup a[href]'
    ];
    clone.querySelectorAll(citationSelectors.join(',')).forEach(
        element => element.remove()
    );
    clone.querySelectorAll('a').forEach(anchor => {
        if (anchor.querySelector('img') && (anchor.textContent || '').trim().length < 80) {
            anchor.remove();
        }
    });

    const wrapper = document.createElement('div');
    wrapper.style.cssText = [
        'position:fixed',
        'left:-100000px',
        'top:0',
        `width:${Math.max(node.getBoundingClientRect().width, 320)}px`,
        'visibility:hidden',
        'pointer-events:none'
    ].join(';');
    wrapper.appendChild(clone);
    document.body.appendChild(wrapper);
    try {
        return (clone.innerText || clone.textContent || '').trim();
    } finally {
        wrapper.remove();
    }
}"""
EXPORT_TURNS_SCRIPT = """() => Array.from(
    document.querySelectorAll('main [data-message-author-role]')
).map(node => ({
    role: node.getAttribute('data-message-author-role') || '',
    text: (node.innerText || '').trim(),
    images: node.querySelectorAll('img').length
})).filter(item => item.role && (item.text || item.images))"""
IMAGE_REQUEST = re.compile(
    r"(?:"
    r"(?:生成|画|绘制|设计|制作|创建|重做|重画|换).{0,16}(?:图片|图像|图|头像|照片)"
    r"|(?:图片|图像|图|头像|照片).{0,16}(?:生成|画|绘制|设计|制作|创建|重做|重画|换)"
    r"|(?:generate|draw|design|create|redesign).{0,32}(?:image|picture|photo|avatar)"
    r")",
    re.IGNORECASE,
)
IMAGE_EDIT_FOLLOWUP = re.compile(
    r"(?:"
    r"(?:修改|改成|改下|换|调成|调整|加上|添加|去掉|删除|重新|再来)"
    r".{0,24}(?:图片|图像|照片|头像|衣服|头发|发型|背景|眼镜|胡子|胡渣|"
    r"年龄|年纪|颜色|风格|嘴唇)"
    r"|(?:图片|图像|照片|头像|衣服|头发|发型|背景|眼镜|胡子|胡渣|年龄|"
    r"年纪|颜色|风格|嘴唇).{0,24}(?:修改|改|换|调|加|去|删|重新|再来)"
    r"|(?:modify|change|edit|adjust|redesign).{0,32}"
    r"(?:image|picture|photo|avatar|clothes|hair|background|glasses|beard)"
    r")",
    re.IGNORECASE,
)
IMAGE_PROGRESS_TEXT = re.compile(
    r"(?:Creating|Generating|Editing)\s+(?:an?\s+)?image|"
    r"(?:正在|仍在)(?:创建|生成|编辑|修改|设计).{0,12}(?:图片|图像|图)|"
    r"(?:图片|图像).{0,8}(?:生成中|编辑中|处理中)",
    re.IGNORECASE,
)
INTERNAL_CITATION = re.compile(
    r"\ue200(?:cite|navlist)\ue202.*?\ue201",
    re.IGNORECASE | re.DOTALL,
)
INTERNAL_TURN_REFERENCE = re.compile(
    r"(?:\[\s*)?turn\d+(?:search|news|open|fetch|view|finance)\d+"
    r"(?:\s*[,、]\s*turn\d+(?:search|news|open|fetch|view|finance)\d+)*"
    r"(?:\s*\])?",
    re.IGNORECASE,
)
MARKDOWN_LINK = re.compile(
    r"\[([^\]\n]+)\]\((?:https?://|www\.)[^)\s]+(?:\s+\"[^\"]*\")?\)",
    re.IGNORECASE,
)
BARE_URL = re.compile(r"(?:https?://|www\.)[^\s<>()，。！？；、]+", re.IGNORECASE)
TRAILING_SOURCE_SECTION = re.compile(
    r"\n(?:#{1,6}\s*)?(?:sources?|来源|参考资料|参考来源|引用)\s*[:：]?\s*\n.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_usable_generated_image(metadata: Any) -> bool:
    """Reject citation icons and other inline images from assistant replies."""
    if not isinstance(metadata, dict):
        return False
    try:
        return bool(
            metadata.get("usable")
            and metadata.get("generated")
            and float(metadata.get("naturalWidth") or 0)
            >= MIN_REPLY_IMAGE_NATURAL_SIZE
            and float(metadata.get("naturalHeight") or 0)
            >= MIN_REPLY_IMAGE_NATURAL_SIZE
            and float(metadata.get("renderedWidth") or 0)
            >= MIN_REPLY_IMAGE_DISPLAY_SIZE
            and float(metadata.get("renderedHeight") or 0)
            >= MIN_REPLY_IMAGE_DISPLAY_SIZE
        )
    except (TypeError, ValueError):
        return False


def _prompt_expects_image(prompt: str) -> bool:
    return bool(IMAGE_REQUEST.search(prompt) or IMAGE_EDIT_FOLLOWUP.search(prompt))


def _normalize_activity_text(value: str) -> str:
    """Remove live timers so only meaningful tool/reply changes renew a wait."""
    text = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "<time>", value)
    text = re.sub(
        r"(?:\b\d+\s*(?:milliseconds?|seconds?|secs?|minutes?|mins?|"
        r"hours?|hrs?|[hms])\b|\d+\s*(?:毫秒|秒钟?|分钟|小时))",
        "<duration>",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split())[-4000:]


def _filter_reply_text(value: Any) -> str:
    """Return readable reply text without web-only citations or source links."""
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = INTERNAL_CITATION.sub("", text)
    text = INTERNAL_TURN_REFERENCE.sub("", text)
    text = MARKDOWN_LINK.sub(r"\1", text)
    text = TRAILING_SOURCE_SECTION.sub("", text)
    text = BARE_URL.sub("", text)
    lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            lines.append("")
        elif line.strip(" \t-*•·—:：()（）[]【】"):
            lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _extract_reply_text(reply: Any) -> str:
    try:
        text = reply.evaluate(REPLY_TEXT_SCRIPT)
    except Exception:
        try:
            text = reply.inner_text(timeout=2_000)
        except Exception:
            return ""
    return _filter_reply_text(text)


@dataclass(frozen=True)
class _ImageBaseline:
    prior_turn_ids: frozenset[str] = frozenset()
    max_turn_number: int = -1
    media_keys: frozenset[str] = frozenset()


def _persistent_context_options(
    profile_dir: Path,
    browser_channel: str,
    proxy_server: str | None,
    *,
    hide_window: bool,
) -> tuple[dict[str, Any], bool]:
    """Build launch options, using a hidden headful window on Windows.

    ChatGPT may treat Chrome's real headless mode as logged out even when the
    persistent profile is valid. An off-screen headful window keeps normal
    Chrome behavior; the native window is hidden immediately after launch.
    """
    hide_native_window = hide_window and os.name == "nt"
    options: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "channel": browser_channel,
        "headless": hide_window and not hide_native_window,
        "viewport": {"width": 1280, "height": 900},
    }
    if hide_native_window:
        options["args"] = [
            "--window-position=-32000,-32000",
            "--window-size=1280,900",
        ]
    if proxy_server:
        options["proxy"] = {"server": proxy_server}
    return options, hide_native_window


def _hide_native_browser_window(page: Any) -> bool:
    """Hide the Chrome window containing *page* without affecting rendering."""
    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    token = f"wechat-codex-hidden-{os.getpid()}-{time.monotonic_ns()}"
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_callback = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    user32.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL

    deadline = time.monotonic() + 5
    title_set = False
    while time.monotonic() < deadline:
        if not title_set:
            try:
                page.evaluate("title => document.title = title", token)
                title_set = True
            except Exception:
                page.wait_for_timeout(50)
                continue

        found = False

        @enum_callback
        def find_window(hwnd: int, _lparam: int) -> bool:
            nonlocal found
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, len(buffer))
            if token not in buffer.value:
                return True
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
            found = True
            return False

        user32.EnumWindows(find_window, 0)
        if found:
            return True
        page.wait_for_timeout(50)

    log.warning("未找到需要隐藏的 Chrome 窗口；窗口已移到屏幕外")
    return False


class ChatStopped(RuntimeError):
    pass


class BrowserSession(Protocol):
    def __enter__(self) -> BrowserSession: ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...

    def ask(
        self,
        conversation_url: str | None,
        prompt: str,
        conversation_title: str | None,
        timeout_seconds: int,
        stopped: Callable[[], bool],
        image_paths: tuple[Path, ...],
        expect_image: bool | None = None,
    ) -> tuple[str, str, tuple[Path, ...]]: ...

    def archive(self, conversation_url: str, timeout_seconds: int) -> None: ...

    def rename(
        self,
        conversation_url: str,
        title: str,
        timeout_seconds: int,
    ) -> None: ...

    def export_markdown(
        self,
        conversation_url: str,
        title: str,
        timeout_seconds: int,
    ) -> str: ...


SessionFactory = Callable[[Path, str, bool, str | None], BrowserSession]


@dataclass(frozen=True)
class ChatEvent:
    text: str
    session_key: str | None = None
    image_paths: tuple[str, ...] = ()
    file_paths: tuple[str, ...] = ()


@dataclass
class ChatState:
    active: bool = False
    session_key: str | None = None
    started_at: float | None = None
    stop_requested: bool = False
    operation: str = "聊天"


@dataclass(frozen=True)
class _AccountState:
    logged_in: bool
    plus: bool


@dataclass(frozen=True)
class _ChatJob:
    session_key: str
    prompt: str
    conversation_url: str | None
    conversation_title: str | None
    image_paths: tuple[Path, ...]
    expect_image: bool


class _ControlKind(str, Enum):
    ARCHIVE = "archive"
    RENAME = "rename"
    RETRY = "retry"
    EXPORT = "export"
    SUMMARIZE = "summarize"


@dataclass(frozen=True)
class _ControlJob:
    session_key: str
    kind: _ControlKind
    conversation_url: str
    value: str = ""
    expect_image: bool = False


def _read_account_state(page: Any) -> _AccountState:
    logged_in = False
    plus = False
    try:
        login_buttons = page.locator(LOGIN_SELECTOR)
        for index in range(login_buttons.count()):
            if login_buttons.nth(index).is_visible(timeout=500):
                return _AccountState(logged_in=False, plus=False)

        profiles = page.locator(ACCOUNT_SELECTOR)
        for index in range(profiles.count()):
            profile = profiles.nth(index)
            if not profile.is_visible(timeout=500):
                continue
            logged_in = True
            try:
                text = profile.inner_text(timeout=500)
            except Exception:
                text = ""
            try:
                label = profile.get_attribute("aria-label", timeout=500) or ""
            except Exception:
                label = ""
            plus = plus or "plus" in f"{text} {label}".lower()
    except Exception:
        log.debug("读取 ChatGPT 账号状态失败", exc_info=True)
    return _AccountState(logged_in=logged_in, plus=plus)


def _wait_for_account_state(page: Any, timeout_ms: int) -> _AccountState:
    deadline = time.monotonic() + timeout_ms / 1000
    last_state = _AccountState(logged_in=False, plus=False)
    while time.monotonic() < deadline:
        last_state = _read_account_state(page)
        if last_state.plus:
            return last_state
        page.wait_for_timeout(250)
    return last_state


class PlaywrightChatSession:
    """One long-lived browser process backed by a persistent Plus profile."""

    def __init__(
        self,
        profile_dir: Path,
        channel: str,
        headless: bool,
        proxy_server: str | None,
    ) -> None:
        self.profile_dir = profile_dir
        self.channel = channel
        self.headless = headless
        self.proxy_server = proxy_server
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._captured_media_keys: set[str] = set()
        self._saved_image_hashes: set[str] | None = None

    def __enter__(self) -> PlaywrightChatSession:
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            launch_options, hide_native_window = _persistent_context_options(
                self.profile_dir,
                self.channel,
                self.proxy_server,
                hide_window=self.headless,
            )
            self._context = self._playwright.chromium.launch_persistent_context(
                **launch_options
            )
            self._context.set_default_timeout(15_000)
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
            if hide_native_window:
                _hide_native_browser_window(self._page)
            self._navigate(self._page, CHATGPT_HOME, 60)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                log.debug("关闭 ChatGPT 浏览器失败", exc_info=True)
            self._context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                log.debug("停止 Playwright 失败", exc_info=True)
            self._playwright = None

    def ask(
        self,
        conversation_url: str | None,
        prompt: str,
        conversation_title: str | None,
        timeout_seconds: int,
        stopped: Callable[[], bool],
        image_paths: tuple[Path, ...],
        expect_image: bool | None = None,
    ) -> tuple[str, str, tuple[Path, ...]]:
        page = self._page
        if page is None:
            raise RuntimeError("ChatGPT 浏览器尚未启动")

        target = (
            conversation_url
            if conversation_url is not None and _is_chat_url(conversation_url)
            else CHATGPT_HOME
        )
        self._navigate(page, target, timeout_seconds)
        try:
            composer = self._wait_for_composer(page)
        except RuntimeError:
            if not conversation_url:
                raise
            log.warning("原 ChatGPT 网页对话不可用，改为创建新对话：%s", conversation_url)
            self._navigate(page, CHATGPT_HOME, timeout_seconds)
            composer = self._wait_for_composer(page)

        if stopped():
            raise ChatStopped("ChatGPT 请求已停止")

        assistant_messages = page.locator(
            f"{ASSISTANT_SELECTOR}, {ASSISTANT_FALLBACK_SELECTOR}"
        )
        previous_count = assistant_messages.count()
        if image_paths:
            self._upload_images(page, image_paths)
        image_baseline = self._image_baseline(
            page,
            usable_only=False,
            exclude_user=False,
        )
        effective_expect_image = (
            _prompt_expects_image(prompt) if expect_image is None else expect_image
        )
        log.info(
            "等待 ChatGPT 回复：图片模式=%s，轮次边界=%d，已存在轮次=%d，历史图片=%d",
            effective_expect_image,
            image_baseline.max_turn_number,
            len(image_baseline.prior_turn_ids),
            len(image_baseline.media_keys),
        )
        composer.fill(prompt)
        send_button = page.locator(SEND_SELECTOR).first
        send_button.wait_for(state="visible", timeout=30_000)
        send_button.click(timeout=30_000)

        text = self._wait_for_reply(
            page,
            assistant_messages,
            previous_count,
            timeout_seconds,
            stopped,
            image_baseline,
            expect_image=effective_expect_image,
        )
        image_paths = self._save_reply_images(
            page,
            image_baseline,
        )
        conversation_url = self._wait_for_conversation_url(page)
        if conversation_title:
            try:
                self._rename_conversation(page, conversation_url, conversation_title)
            except Exception as exc:
                log.warning(
                    "ChatGPT 对话已创建并可继续使用，但标题更新失败（%s）：%s",
                    conversation_title,
                    exc,
                )
        return text, conversation_url, image_paths

    def archive(self, conversation_url: str, timeout_seconds: int) -> None:
        page = self._page
        if page is None:
            raise RuntimeError("ChatGPT 浏览器尚未启动")
        self._navigate(page, conversation_url, timeout_seconds)
        self._wait_for_composer(page)
        self._open_conversation_menu(page, conversation_url)
        archive_pattern = re.compile(
            r"^(?:Archive|Archive chat|归档|归档对话)$",
            re.IGNORECASE,
        )
        archive_item = page.get_by_role("menuitem", name=archive_pattern)
        if archive_item.count() == 0:
            archive_item = page.get_by_text(archive_pattern)
        if archive_item.count() == 0:
            raise RuntimeError("找不到 ChatGPT 对话归档菜单")
        archive_item.last.click()
        page.wait_for_timeout(750)

    def rename(
        self,
        conversation_url: str,
        title: str,
        timeout_seconds: int,
    ) -> None:
        page = self._page
        if page is None:
            raise RuntimeError("ChatGPT 浏览器尚未启动")
        self._navigate(page, conversation_url, timeout_seconds)
        self._wait_for_composer(page)
        self._rename_conversation(page, conversation_url, title)

    def export_markdown(
        self,
        conversation_url: str,
        title: str,
        timeout_seconds: int,
    ) -> str:
        page = self._page
        if page is None:
            raise RuntimeError("ChatGPT 浏览器尚未启动")
        self._navigate(page, conversation_url, timeout_seconds)
        self._wait_for_composer(page)
        turns = page.evaluate(EXPORT_TURNS_SCRIPT)
        if not isinstance(turns, list) or not turns:
            raise RuntimeError("当前 ChatGPT 对话没有可导出的内容")

        sections = [
            f"# {title}",
            "",
            f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- ChatGPT 地址：{conversation_url}",
            "",
        ]
        role_names = {"user": "用户", "assistant": "ChatGPT"}
        for raw in turns:
            if not isinstance(raw, dict):
                continue
            role = role_names.get(str(raw.get("role") or ""), "系统")
            body = str(raw.get("text") or "").strip()
            image_count = int(raw.get("images") or 0)
            if image_count:
                image_note = f"[图片 {image_count} 张]"
                body = f"{body}\n\n{image_note}" if body else image_note
            if body:
                sections.extend((f"## {role}", "", body, ""))
        if len(sections) <= 5:
            raise RuntimeError("当前 ChatGPT 对话没有可导出的文字或图片")
        return "\n".join(sections).rstrip() + "\n"

    @staticmethod
    def _upload_images(page: Any, image_paths: tuple[Path, ...]) -> None:
        missing = [str(path) for path in image_paths if not path.is_file()]
        if missing:
            raise RuntimeError(f"待上传的微信图片不存在：{missing[0]}")

        inputs = page.locator(FILE_INPUT_SELECTOR)
        deadline = time.monotonic() + 5
        while inputs.count() == 0 and time.monotonic() < deadline:
            page.wait_for_timeout(100)
            inputs = page.locator(FILE_INPUT_SELECTOR)
        if inputs.count() == 0:
            raise RuntimeError("找不到 ChatGPT 图片上传控件；网页结构可能已经更新")

        selected = inputs.last
        selected.set_input_files([str(path) for path in image_paths])

    @staticmethod
    def _rename_conversation(page: Any, conversation_url: str, title: str) -> None:
        """Rename the current web conversation through ChatGPT's visible UI."""
        cleaned = " ".join(title.split()).strip()[:80]
        if not cleaned:
            return
        anchor, row = PlaywrightChatSession._open_conversation_menu(
            page,
            conversation_url,
            open_menu=False,
        )
        try:
            current_text = " ".join(anchor.inner_text(timeout=1_000).split())
        except Exception:
            current_text = ""
        if current_text == cleaned:
            return
        PlaywrightChatSession._click_conversation_menu(anchor, row)

        rename_pattern = re.compile(r"^(?:Rename|重命名|重新命名)$", re.IGNORECASE)
        rename_item = page.get_by_role("menuitem", name=rename_pattern)
        if rename_item.count() == 0:
            rename_item = page.get_by_text(rename_pattern)
        if rename_item.count() == 0:
            raise RuntimeError("找不到 ChatGPT 对话重命名菜单")
        rename_item.last.click()

        editor = page.locator(
            '[role="dialog"] input, '
            'input[data-testid*="rename"], '
            'input[aria-label*="rename" i], '
            'input[aria-label*="重命名"]'
        )
        if editor.count() == 0:
            editor = row.locator("input")
        if editor.count() == 0:
            raise RuntimeError("找不到 ChatGPT 对话标题输入框")
        editor = editor.last
        editor.wait_for(state="visible", timeout=3_000)
        editor.fill(cleaned)
        editor.press("Enter")

    @staticmethod
    def _open_conversation_menu(
        page: Any,
        conversation_url: str,
        *,
        open_menu: bool = True,
    ) -> tuple[Any, Any]:
        from urllib.parse import urlparse

        path = urlparse(conversation_url).path
        if not path:
            raise RuntimeError("无法从对话地址确定侧边栏项目")
        anchor = page.locator(f'a[href="{path}"]')
        if anchor.count() == 0:
            sidebar_button = page.locator(
                'button[data-testid="open-sidebar-button"], '
                'button[aria-label*="sidebar" i], '
                'button[aria-label*="边栏"]'
            ).first
            if sidebar_button.count() and sidebar_button.is_visible(timeout=500):
                sidebar_button.click()
                page.wait_for_timeout(300)
                anchor = page.locator(f'a[href="{path}"]')
        deadline = time.monotonic() + 5
        while anchor.count() == 0 and time.monotonic() < deadline:
            page.wait_for_timeout(250)
            anchor = page.locator(f'a[href="{path}"]')
        if anchor.count() == 0:
            raise RuntimeError("侧边栏中找不到当前 ChatGPT 对话")
        anchor = anchor.first
        anchor.hover()
        row = anchor.locator("xpath=ancestor::*[self::li or @data-testid][1]")
        if row.count() == 0:
            row = anchor.locator("xpath=..")
        if open_menu:
            PlaywrightChatSession._click_conversation_menu(anchor, row)
        return anchor, row

    @staticmethod
    def _click_conversation_menu(anchor: Any, row: Any) -> None:
        anchor.hover()
        options = row.locator(
            'button[aria-label*="option" i], '
            'button[aria-label*="more" i], '
            'button[aria-label*="选项"], '
            'button[aria-label*="更多"], '
            'button[data-testid*="menu"]'
        )
        if options.count() == 0:
            options = row.locator("button")
        if options.count() == 0:
            raise RuntimeError("找不到 ChatGPT 对话选项按钮")
        options.last.click()

    @staticmethod
    def _navigate(page: Any, url: str, timeout_seconds: int) -> None:
        timeout_ms = min(timeout_seconds, 45) * 1000
        retryable_errors = (
            "ERR_ABORTED",
            "ERR_CONNECTION_CLOSED",
            "ERR_CONNECTION_RESET",
            "ERR_CONNECTION_TIMED_OUT",
            "ERR_PROXY_CONNECTION_FAILED",
            "ERR_SOCKS_CONNECTION_FAILED",
            "frame was detached",
        )
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                page.goto(url, wait_until="commit", timeout=timeout_ms)
                return
            except Exception as exc:
                detail = str(exc)
                if not any(marker in detail for marker in retryable_errors):
                    raise
                interrupted_navigation = (
                    "ERR_ABORTED" in detail or "frame was detached" in detail
                )
                if interrupted_navigation and str(page.url).startswith(
                    "https://chatgpt.com/"
                ):
                    return
                if attempt == max_attempts - 1:
                    raise
                delay_ms = 1000 * (2**attempt)
                log.warning(
                    "ChatGPT 页面连接失败，%s 毫秒后重试（%d/%d）：%s",
                    delay_ms,
                    attempt + 1,
                    max_attempts,
                    detail.splitlines()[0],
                )
                page.wait_for_timeout(delay_ms)

    @staticmethod
    def _wait_for_composer(page: Any) -> Any:
        account = _wait_for_account_state(page, timeout_ms=10_000)
        if not account.logged_in:
            raise RuntimeError("ChatGPT Plus 尚未登录，请先运行 chatgpt-login")
        if not account.plus:
            raise RuntimeError("当前 ChatGPT 账号未检测到 Plus 订阅")
        composer = page.locator(PROMPT_SELECTOR).first
        try:
            composer.wait_for(state="visible", timeout=20_000)
            return composer
        except Exception as exc:
            url = str(page.url)
            if "auth" in url or "login" in url:
                raise RuntimeError(
                    "ChatGPT Plus 尚未登录，请先运行 chatgpt-login"
                ) from exc
            raise RuntimeError(
                "找不到 ChatGPT 输入框；请确认已登录，或网页结构可能已经更新"
            ) from exc

    @staticmethod
    def _wait_for_reply(
        page: Any,
        assistant_messages: Any,
        previous_count: int,
        timeout_seconds: int,
        stopped: Callable[[], bool],
        image_baseline: _ImageBaseline = _ImageBaseline(),
        *,
        expect_image: bool = False,
    ) -> str:
        started_at = time.monotonic()
        initial_deadline = started_at + timeout_seconds
        deadline = initial_deadline
        last_text = ""
        last_media_keys: frozenset[str] = frozenset()
        last_activity_key: tuple[tuple[str, str, int], ...] = ()
        unchanged_since: float | None = None
        image_tool_seen = False
        extension_logged = False
        saw_tool_activity = False

        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if stopped():
                PlaywrightChatSession._stop_generation(page)
                raise ChatStopped("ChatGPT 请求已停止")

            count = assistant_messages.count()
            new_media_keys = PlaywrightChatSession._new_generated_media_keys(
                page,
                image_baseline,
            )
            activity_key = PlaywrightChatSession._reply_activity_key(
                page,
                image_baseline,
            )
            image_count = len(new_media_keys)
            current = ""
            reply = PlaywrightChatSession._current_reply_assistant(
                page,
                image_baseline,
            )
            if reply is None and count > previous_count:
                reply = assistant_messages.nth(count - 1)
            if reply is not None:
                current = _extract_reply_text(reply)
                image_count = max(
                    image_count,
                    PlaywrightChatSession._visible_reply_image_count(reply),
                )
            content_changed = (
                current != last_text or new_media_keys != last_media_keys
            ) and bool(current or new_media_keys or last_text or last_media_keys)
            activity_changed = bool(
                activity_key and activity_key != last_activity_key
            )
            if activity_changed:
                last_activity_key = activity_key
                saw_tool_activity = True
            if content_changed:
                last_text = current
                last_media_keys = new_media_keys
            if content_changed or activity_changed:
                unchanged_since = now
                # Treat the configured timeout as an inactivity limit once
                # ChatGPT starts streaming or a web/tool turn changes.
                deadline = now + timeout_seconds
                if now >= initial_deadline and not extension_logged:
                    log.info("ChatGPT 回复仍在更新，已根据最近活动自动延长等待")
                    extension_logged = True

            if current or image_count:
                if unchanged_since is None:
                    last_text = current
                    last_media_keys = new_media_keys
                    unchanged_since = now
                    deadline = now + timeout_seconds
                else:
                    generating = page.locator(STOP_SELECTOR).first.is_visible(
                        timeout=500
                    )
                    image_progress = PlaywrightChatSession._image_progress_visible(page)
                    image_tool_seen = image_tool_seen or image_progress
                    settle_seconds = (
                        8.0 if expect_image or image_tool_seen or image_count else 1.5
                    )
                    if (
                        not generating
                        and not image_progress
                        and now - unchanged_since >= settle_seconds
                    ):
                        return current

            page.wait_for_timeout(250)

        if last_text or last_media_keys:
            raise TimeoutError(
                f"ChatGPT 回复连续 {timeout_seconds} 秒没有继续更新，等待超时"
            )
        if saw_tool_activity:
            raise TimeoutError(
                f"ChatGPT 搜索或工具连续 {timeout_seconds} 秒没有继续更新，等待超时"
            )
        raise TimeoutError("等待 ChatGPT 回复超时")

    @staticmethod
    def _stop_generation(page: Any) -> None:
        """Best-effort cancellation of the active web generation."""
        try:
            button = page.locator(STOP_SELECTOR).first
            if button.is_visible(timeout=500):
                button.click(timeout=2_000)
        except Exception:
            log.debug("点击 ChatGPT 停止生成按钮失败", exc_info=True)

    @staticmethod
    def _visible_reply_image_count(reply: Any) -> int:
        try:
            images = reply.locator("img")
            return sum(
                1
                for index in range(images.count())
                if _is_usable_generated_image(
                    images.nth(index).evaluate(IMAGE_METADATA_SCRIPT)
                )
            )
        except Exception:
            return 0

    @staticmethod
    def _image_progress_visible(page: Any) -> bool:
        try:
            turns = page.locator('main [data-testid^="conversation-turn-"]')
            start = max(0, turns.count() - 2)
            for index in range(start, turns.count()):
                text = turns.nth(index).inner_text(timeout=500)
                if IMAGE_PROGRESS_TEXT.search(text):
                    return True
            return False
        except Exception:
            return False

    @staticmethod
    def _image_baseline(
        page: Any,
        *,
        usable_only: bool = True,
        exclude_user: bool = True,
    ) -> _ImageBaseline:
        try:
            turns = page.locator('main [data-testid^="conversation-turn-"]')
            prior_turn_ids: set[str] = set()
            max_turn_number = -1
            for index in range(turns.count()):
                turn_id = str(
                    turns.nth(index).get_attribute("data-testid") or ""
                )
                if not turn_id:
                    continue
                prior_turn_ids.add(turn_id)
                match = re.search(r"(\d+)$", turn_id)
                if match:
                    max_turn_number = max(max_turn_number, int(match.group(1)))

            images = page.locator(GENERATED_IMAGE_SELECTOR)
            media_keys: set[str] = set()
            for index in range(images.count()):
                metadata = images.nth(index).evaluate(IMAGE_METADATA_SCRIPT)
                if (
                    (_is_usable_generated_image(metadata) or not usable_only)
                    and (not metadata.get("userTurn") or not exclude_user)
                ):
                    if metadata.get("mediaKey"):
                        media_keys.add(str(metadata["mediaKey"]))
            return _ImageBaseline(
                frozenset(prior_turn_ids),
                max_turn_number,
                frozenset(media_keys),
            )
        except Exception:
            return _ImageBaseline()

    @staticmethod
    def _new_generated_media_keys(
        page: Any,
        baseline: _ImageBaseline,
    ) -> frozenset[str]:
        try:
            media_keys: set[str] = set()
            for image in PlaywrightChatSession._current_reply_images(page, baseline):
                metadata = image.evaluate(IMAGE_METADATA_SCRIPT)
                turn_id = str(metadata.get("turnId") or "")
                media_key = str(metadata.get("mediaKey") or "")
                if (
                    _is_usable_generated_image(metadata)
                    and not metadata.get("userTurn")
                    and media_key
                    and PlaywrightChatSession._is_current_turn(turn_id, baseline)
                    and media_key not in baseline.media_keys
                ):
                    media_keys.add(media_key)
            return frozenset(media_keys)
        except Exception:
            return frozenset()

    @staticmethod
    def _current_reply_turns(page: Any, baseline: _ImageBaseline) -> list[Any]:
        """Return recent DOM turns created after the request baseline."""
        turns = page.locator('main [data-testid^="conversation-turn-"]')
        result: list[Any] = []
        count = turns.count()
        # Tool use can create several adjacent turns; scan a bounded tail to
        # support long conversations whose older DOM nodes are virtualized.
        for index in range(max(0, count - 10), count):
            turn = turns.nth(index)
            turn_id = str(turn.get_attribute("data-testid") or "")
            if PlaywrightChatSession._is_current_turn(turn_id, baseline):
                result.append(turn)
        return result

    @staticmethod
    def _current_reply_assistant(page: Any, baseline: _ImageBaseline) -> Any | None:
        """Find the newest assistant node by turn id, not by total node count."""
        try:
            selector = f"{ASSISTANT_SELECTOR}, {ASSISTANT_FALLBACK_SELECTOR}"
            for turn in reversed(
                PlaywrightChatSession._current_reply_turns(page, baseline)
            ):
                if (
                    turn.get_attribute("data-message-author-role") == "assistant"
                    or turn.get_attribute("data-turn") == "assistant"
                ):
                    return turn
                assistants = turn.locator(selector)
                if assistants.count():
                    return assistants.last
            return None
        except Exception:
            return None

    @staticmethod
    def _reply_activity_key(
        page: Any,
        baseline: _ImageBaseline,
    ) -> tuple[tuple[str, str, int], ...]:
        """Fingerprint new assistant/tool turns to renew an active wait."""
        try:
            result: list[tuple[str, str, int]] = []
            for turn in PlaywrightChatSession._current_reply_turns(page, baseline):
                turn_id = str(turn.get_attribute("data-testid") or "")
                try:
                    text = _normalize_activity_text(turn.inner_text(timeout=500))
                except Exception:
                    text = ""
                try:
                    element_count = int(
                        turn.evaluate("node => node.querySelectorAll('*').length")
                    )
                except Exception:
                    element_count = 0
                result.append((turn_id, text, element_count))
            return tuple(result)
        except Exception:
            return ()

    @staticmethod
    def _current_reply_images(page: Any, baseline: _ImageBaseline) -> list[Any]:
        """Return images from only the newest turns created after the baseline."""
        result: list[Any] = []
        for turn in PlaywrightChatSession._current_reply_turns(page, baseline):
            images = turn.locator("img")
            result.extend(images.nth(image_index) for image_index in range(images.count()))
        return result

    @staticmethod
    def _is_current_turn(turn_id: str, baseline: _ImageBaseline) -> bool:
        if not turn_id or turn_id in baseline.prior_turn_ids:
            return False
        match = re.search(r"(\d+)$", turn_id)
        if match and int(match.group(1)) <= baseline.max_turn_number:
            return False
        return True

    def _save_reply_images(
        self,
        page: Any,
        baseline: _ImageBaseline,
    ) -> tuple[Path, ...]:
        output_dir = self.profile_dir.parent / "chatgpt-images"
        self._load_saved_image_hashes(output_dir)
        saved: list[Path] = []
        seen_images: set[str] = set()
        try:
            images = self._current_reply_images(page, baseline)
        except Exception:
            return ()

        candidates = len(images)
        for image in images:
            self._save_reply_image(
                image,
                output_dir,
                baseline,
                seen_images,
                saved,
            )
        log.info(
            "ChatGPT 当前轮次图片扫描完成：整页候选=%d，轮次边界=%d，已保存=%d",
            candidates,
            baseline.max_turn_number,
            len(saved),
        )
        return tuple(saved)

    def _save_reply_image(
        self,
        image: Any,
        output_dir: Path,
        baseline: _ImageBaseline,
        seen_images: set[str],
        saved: list[Path],
    ) -> bool:
        try:
            metadata = image.evaluate(IMAGE_METADATA_SCRIPT)
        except Exception:
            return False
        if not _is_usable_generated_image(metadata) or metadata.get("userTurn"):
            return False
        source = str(metadata.get("source") or "")
        turn_id = str(metadata.get("turnId") or "")
        media_key = str(metadata.get("mediaKey") or "")
        if (
            not source
            or not media_key
            or not self._is_current_turn(turn_id, baseline)
            or media_key in baseline.media_keys
            or media_key in self._captured_media_keys
            or media_key in seen_images
        ):
            return False
        seen_images.add(media_key)
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / f"{uuid.uuid4().hex}.png"
        try:
            encoded = image.evaluate(
                """async img => {
                    try {
                        const response = await fetch(
                            img.currentSrc || img.src,
                            {credentials: 'include'}
                        );
                        if (!response.ok) return null;
                        const blob = await response.blob();
                        if (!blob.size || blob.size > 20971520) return null;
                        return await new Promise((resolve, reject) => {
                            const reader = new FileReader();
                            reader.onload = () => resolve(reader.result);
                            reader.onerror = reject;
                            reader.readAsDataURL(blob);
                        });
                    } catch (_) {
                        return null;
                    }
                }"""
            )
            raw, suffix = self._decode_image_data_url(encoded)
            destination = destination.with_suffix(suffix)
            destination.write_bytes(raw)
        except Exception:
            try:
                image.screenshot(path=str(destination), type="png")
            except Exception:
                log.warning("保存 ChatGPT 回复图片失败", exc_info=True)
                return False
        digest = _file_sha256(destination)
        assert self._saved_image_hashes is not None
        self._captured_media_keys.add(media_key)
        if digest in self._saved_image_hashes:
            destination.unlink(missing_ok=True)
            log.info("忽略与历史缓存内容相同的 ChatGPT 图片：%s", media_key)
            return False
        self._saved_image_hashes.add(digest)
        saved.append(destination.resolve())
        return True

    def _load_saved_image_hashes(self, output_dir: Path) -> None:
        if self._saved_image_hashes is not None:
            return
        hashes: set[str] = set()
        if output_dir.is_dir():
            for path in output_dir.iterdir():
                if not path.is_file():
                    continue
                try:
                    hashes.add(_file_sha256(path))
                except OSError:
                    log.warning("读取 ChatGPT 历史图片缓存失败：%s", path)
        self._saved_image_hashes = hashes

    @staticmethod
    def _decode_image_data_url(value: Any) -> tuple[bytes, str]:
        if not isinstance(value, str) or not value.startswith("data:image/"):
            raise ValueError("ChatGPT 图片没有可读取的数据")
        header, separator, encoded = value.partition(",")
        if not separator or ";base64" not in header.lower():
            raise ValueError("ChatGPT 图片不是 Base64 数据")
        media_type = header[5:].split(";", 1)[0].lower()
        suffixes = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        suffix = suffixes.get(media_type)
        if suffix is None:
            raise ValueError(f"不支持的 ChatGPT 图片格式：{media_type}")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("ChatGPT 图片 Base64 数据无效") from exc
        if not raw or len(raw) > MAX_REPLY_IMAGE_BYTES:
            raise ValueError("ChatGPT 图片为空或超过 20 MB")
        return raw, suffix

    @staticmethod
    def _wait_for_conversation_url(page: Any) -> str:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            url = str(page.url)
            if _is_chat_url(url):
                return url
            page.wait_for_timeout(100)
        raise RuntimeError("ChatGPT 已回复，但未获得可继续的对话地址")


class ChatGPTRunner:
    def __init__(
        self,
        *,
        browser_channel: str,
        headless: bool,
        proxy_server: str | None,
        timeout_seconds: int,
        runtime_dir: Path,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.browser_channel = browser_channel
        self.headless = headless
        self.proxy_server = proxy_server
        self.timeout_seconds = timeout_seconds
        self.runtime_dir = runtime_dir
        self.profile_dir = runtime_dir / "chatgpt-plus-profile"
        self.events: queue.Queue[ChatEvent] = queue.Queue()
        self._session_factory = session_factory or PlaywrightChatSession
        self._state = ChatState()
        self._lock = threading.RLock()
        self._jobs: queue.Queue[_ChatJob | _ControlJob | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_ready = threading.Event()
        self._startup_error: Exception | None = None
        self._closing = False
        self._conversation_file = runtime_dir / "chatgpt_web_conversations.json"
        self._title_file = runtime_dir / "chatgpt_conversation_titles.json"
        self._history_file = runtime_dir / "chatgpt_conversation_history.json"
        self._export_dir = runtime_dir / "chatgpt-exports"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._conversations = self._load_conversations()
        self._titles = self._load_titles()
        self._history = self._load_history()
        self._last_replies: dict[str, ChatEvent] = {}
        self._last_image_requests: dict[str, bool] = {}
        self._seed_history_from_current()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state.active

    @property
    def browser_running(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def start(self) -> None:
        """Start one dedicated browser and keep it alive for this runner."""
        with self._lock:
            if self._closing:
                raise RuntimeError("ChatGPT 浏览器已经关闭")
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker_ready.clear()
            self._startup_error = None
            worker = threading.Thread(
                target=self._browser_loop,
                name="chatgpt-plus-browser",
                daemon=True,
            )
            self._worker = worker
            worker.start()

        if not self._worker_ready.wait(60):
            raise RuntimeError("等待 ChatGPT 专用浏览器启动超时")
        if self._startup_error is not None:
            detail = str(self._startup_error).strip() or type(self._startup_error).__name__
            raise RuntimeError(f"ChatGPT 专用浏览器启动失败：{detail}")

    def begin_chat(
        self,
        session_key: str,
        prompt: str,
        conversation_title: str | None = None,
        image_paths: tuple[str | Path, ...] = (),
    ) -> tuple[bool, str]:
        try:
            self.start()
        except RuntimeError as exc:
            return False, str(exc)

        resolved_images = tuple(Path(path).expanduser().resolve() for path in image_paths)
        missing = [str(path) for path in resolved_images if not path.is_file()]
        if missing:
            return False, f"微信图片文件不存在：{missing[0]}"

        with self._lock:
            if self._state.active:
                return False, "已有 ChatGPT 请求在执行，请发送 @状态 或 @停止"
            conversation_url = self._conversations.get(session_key)
            effective_title = self._titles.get(session_key, conversation_title)
            expect_image = _prompt_expects_image(prompt)
            self._last_image_requests[session_key] = expect_image
            self._state = ChatState(
                active=True,
                session_key=session_key,
                started_at=time.time(),
                operation="聊天",
            )

        self._jobs.put(
            _ChatJob(
                session_key=session_key,
                prompt=prompt,
                conversation_url=conversation_url,
                conversation_title=effective_title,
                image_paths=resolved_images,
                expect_image=expect_image,
            )
        )
        return True, "已开始 ChatGPT Plus 网页请求"

    def begin_reset(self, session_key: str) -> tuple[bool, str]:
        with self._lock:
            if self._state.active:
                return False, "已有 ChatGPT 请求在执行，请发送 @状态 或 @停止"
            if session_key not in self._conversations:
                return False, "当前已经是新的 ChatGPT 对话"
        self._remove_conversation(session_key)
        self._remove_title(session_key)
        self.events.put(
            ChatEvent(
                "已切换到新的 ChatGPT 对话；旧对话仍保留在你的 ChatGPT 历史中",
                session_key,
            )
        )
        return True, "已切换到新的 ChatGPT 对话"

    def begin_archive(self, session_key: str) -> tuple[bool, str]:
        return self._begin_control(session_key, _ControlKind.ARCHIVE, "", "归档对话")

    def begin_rename(self, session_key: str, title: str) -> tuple[bool, str]:
        cleaned = " ".join(title.split()).strip()[:80]
        if not cleaned:
            return False, "对话标题不能为空"
        return self._begin_control(
            session_key,
            _ControlKind.RENAME,
            cleaned,
            "重命名对话",
        )

    def begin_retry(self, session_key: str) -> tuple[bool, str]:
        return self._begin_control(session_key, _ControlKind.RETRY, "", "重试")

    def begin_export(
        self,
        session_key: str,
        default_title: str,
    ) -> tuple[bool, str]:
        with self._lock:
            title = self._titles.get(session_key, default_title)
        return self._begin_control(
            session_key,
            _ControlKind.EXPORT,
            title,
            "导出对话",
        )

    def begin_summary(self, session_key: str) -> tuple[bool, str]:
        return self._begin_control(
            session_key,
            _ControlKind.SUMMARIZE,
            "",
            "总结对话",
        )

    def _begin_control(
        self,
        session_key: str,
        kind: _ControlKind,
        value: str,
        operation: str,
    ) -> tuple[bool, str]:
        try:
            self.start()
        except RuntimeError as exc:
            return False, str(exc)
        with self._lock:
            if self._state.active:
                return False, "已有 ChatGPT 请求在执行，请发送 @状态 或 @停止"
            conversation_url = self._conversations.get(session_key)
            if not conversation_url:
                return False, "当前还没有可操作的 ChatGPT 对话"
            expect_image = (
                self._last_image_requests.get(session_key, False)
                if kind == _ControlKind.RETRY
                else False
            )
            self._state = ChatState(
                active=True,
                session_key=session_key,
                started_at=time.time(),
                operation=operation,
            )
        self._jobs.put(
            _ControlJob(
                session_key,
                kind,
                conversation_url,
                value,
                expect_image,
            )
        )
        return True, f"已开始{operation}"

    def conversation_info(self, session_key: str, default_title: str) -> str:
        with self._lock:
            url = self._conversations.get(session_key)
            title = self._titles.get(session_key, default_title)
        if not url:
            return "当前是新的 ChatGPT 对话，尚未发送第一条消息"
        return f"当前 ChatGPT 对话\n标题：{title}\n地址：{url}"

    def conversation_list(
        self,
        session_key: str,
        default_title: str,
        limit: int = 10,
    ) -> str:
        with self._lock:
            records = [dict(record) for record in self._history.get(session_key, [])]
            current_url = self._conversations.get(session_key)
        if not records:
            return "当前没有保存的 ChatGPT 对话"
        lines = ["最近保存的 ChatGPT 对话："]
        for index, record in enumerate(records[:limit], start=1):
            title = str(record.get("title") or default_title)
            updated_at = float(record.get("updated_at") or 0)
            updated = (
                time.strftime("%m-%d %H:%M", time.localtime(updated_at))
                if updated_at
                else "时间未知"
            )
            flags: list[str] = []
            if str(record.get("url") or "") == current_url:
                flags.append("当前")
            if record.get("archived"):
                flags.append("已归档")
            suffix = f" [{' / '.join(flags)}]" if flags else ""
            lines.append(f"{index}. {title}（{updated}）{suffix}")
        lines.append("发送 @切换对话：编号 以恢复历史对话")
        return "\n".join(lines)

    def switch_conversation(
        self,
        session_key: str,
        number: int,
        default_title: str,
    ) -> tuple[bool, str]:
        with self._lock:
            if self._state.active:
                return False, "已有 ChatGPT 请求在执行，请发送 @状态 或 @停止"
            records = self._history.get(session_key, [])
            if number < 1 or number > len(records):
                return False, f"对话编号无效；当前共有 {len(records)} 个保存的对话"
            record = dict(records[number - 1])
            url = str(record.get("url") or "")
            title = str(record.get("title") or default_title)
            if not _is_chat_url(url):
                return False, "保存的 ChatGPT 对话地址无效"
            self._conversations[session_key] = url
            self._titles[session_key] = title
            conversations = dict(self._conversations)
            titles = dict(self._titles)
        self._save_conversations(conversations)
        self._save_titles(titles)
        self._remember_conversation(session_key, url, title)
        return True, f"已切换到对话：{title}"

    def last_reply(self, session_key: str) -> ChatEvent | None:
        with self._lock:
            event = self._last_replies.get(session_key)
        if event is None:
            return None
        paths = tuple(path for path in event.image_paths if Path(path).is_file())
        files = tuple(path for path in event.file_paths if Path(path).is_file())
        return ChatEvent(event.text, event.session_key, paths, files)

    def protected_image_paths(self) -> frozenset[Path]:
        with self._lock:
            events = tuple(self._last_replies.values())
        return frozenset(
            Path(path).resolve()
            for event in events
            for path in event.image_paths
            if Path(path).is_file()
        )

    def _browser_loop(self) -> None:
        try:
            with self._session_factory(
                self.profile_dir,
                self.browser_channel,
                self.headless,
                self.proxy_server,
            ) as session:
                self._worker_ready.set()
                while True:
                    job = self._jobs.get()
                    if job is None:
                        return
                    if isinstance(job, _ControlJob):
                        self._run_control(session, job)
                    else:
                        self._run_chat(session, job)
        except Exception as exc:
            if not self._worker_ready.is_set():
                self._startup_error = exc
            else:
                detail = str(exc).strip() or type(exc).__name__
                with self._lock:
                    session_key = self._state.session_key
                self.events.put(ChatEvent(
                    f"ChatGPT 专用浏览器意外退出：{detail[-1000:]}", session_key
                ))
                self._finish()
        finally:
            self._worker_ready.set()
            with self._lock:
                if threading.current_thread() is self._worker:
                    self._worker = None

    def _run_chat(self, session: BrowserSession, job: _ChatJob) -> None:
        try:
            text, new_url, image_paths = session.ask(
                job.conversation_url,
                job.prompt,
                job.conversation_title,
                self.timeout_seconds,
                self._stopped,
                job.image_paths,
                expect_image=job.expect_image,
            )

            if self._stopped():
                self.events.put(ChatEvent("ChatGPT 请求已停止", job.session_key))
                return
            self._set_conversation(
                job.session_key,
                new_url,
                job.conversation_title,
            )
            event = ChatEvent(
                text,
                job.session_key,
                tuple(str(path) for path in image_paths),
            )
            with self._lock:
                self._last_replies[job.session_key] = event
            self.events.put(event)
        except ChatStopped:
            self.events.put(ChatEvent("ChatGPT 请求已停止", job.session_key))
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            self.events.put(ChatEvent(
                f"ChatGPT Plus 网页请求失败：{detail[-1000:]}", job.session_key
            ))
        finally:
            self._finish()

    def _run_control(self, session: BrowserSession, job: _ControlJob) -> None:
        try:
            if job.kind == _ControlKind.ARCHIVE:
                session.archive(job.conversation_url, self.timeout_seconds)
                with self._lock:
                    title = self._titles.get(job.session_key, "")
                self._remember_conversation(
                    job.session_key,
                    job.conversation_url,
                    title,
                    archived=True,
                )
                self._remove_conversation(job.session_key)
                self._remove_title(job.session_key)
                self.events.put(ChatEvent(
                    "当前 ChatGPT 对话已归档，下一条消息将创建新对话",
                    job.session_key,
                ))
            elif job.kind == _ControlKind.RENAME:
                session.rename(job.conversation_url, job.value, self.timeout_seconds)
                self._set_title(job.session_key, job.value)
                self.events.put(ChatEvent(
                    f"当前 ChatGPT 对话已重命名为：{job.value}",
                    job.session_key,
                ))
            elif job.kind == _ControlKind.RETRY:
                prompt = (
                    "请重新处理我上一条消息并给出新的回答。"
                    "如果上一条消息要求生成或修改图片，请重新生成图片，"
                    "不要复用之前的图片。"
                )
                title = self._titles.get(job.session_key)
                text, new_url, image_paths = session.ask(
                    job.conversation_url,
                    prompt,
                    title,
                    self.timeout_seconds,
                    self._stopped,
                    (),
                    expect_image=job.expect_image,
                )
                if self._stopped():
                    self.events.put(ChatEvent("ChatGPT 请求已停止", job.session_key))
                    return
                self._set_conversation(job.session_key, new_url, title)
                event = ChatEvent(
                    text,
                    job.session_key,
                    tuple(str(path) for path in image_paths),
                )
                with self._lock:
                    self._last_replies[job.session_key] = event
                self.events.put(event)
            elif job.kind == _ControlKind.EXPORT:
                markdown = session.export_markdown(
                    job.conversation_url,
                    job.value or "ChatGPT 对话",
                    self.timeout_seconds,
                )
                self._export_dir.mkdir(parents=True, exist_ok=True)
                safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", job.value)
                safe_title = safe_title.strip(" ._")[:60] or "ChatGPT对话"
                destination = self._export_dir / (
                    f"{safe_title}-{time.strftime('%Y%m%d-%H%M%S')}-"
                    f"{uuid.uuid4().hex[:6]}.md"
                )
                temporary = destination.with_suffix(".md.tmp")
                temporary.write_text(markdown, encoding="utf-8")
                os.replace(temporary, destination)
                self.events.put(ChatEvent(
                    f"对话已导出：{destination.name}",
                    job.session_key,
                    file_paths=(str(destination.resolve()),),
                ))
            elif job.kind == _ControlKind.SUMMARIZE:
                prompt = (
                    "请总结我们当前整个对话的上下文，包含主要目标、关键结论、"
                    "已经完成的事项、未完成事项和后续建议。使用中文，结构清晰，"
                    "确保这份总结脱离原对话也能独立理解。"
                )
                with self._lock:
                    title = self._titles.get(job.session_key)
                text, new_url, image_paths = session.ask(
                    job.conversation_url,
                    prompt,
                    title,
                    self.timeout_seconds,
                    self._stopped,
                    (),
                    expect_image=False,
                )
                if self._stopped():
                    self.events.put(ChatEvent("ChatGPT 请求已停止", job.session_key))
                    return
                self._set_conversation(job.session_key, new_url, title)
                event = ChatEvent(
                    f"对话总结\n\n{text}\n\n已开启新对话，下一条普通消息将使用新上下文。",
                    job.session_key,
                    tuple(str(path) for path in image_paths),
                )
                with self._lock:
                    self._last_replies[job.session_key] = event
                self._remove_conversation(job.session_key)
                self._remove_title(job.session_key)
                self.events.put(event)
        except ChatStopped:
            self.events.put(ChatEvent("ChatGPT 请求已停止", job.session_key))
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            self.events.put(ChatEvent(
                f"ChatGPT {self._state.operation}失败：{detail[-1000:]}",
                job.session_key,
            ))
        finally:
            self._finish()

    def _load_conversations(self) -> dict[str, str]:
        if not self._conversation_file.is_file():
            return {}
        try:
            raw = json.loads(self._conversation_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("顶层不是 JSON 对象")
            return {
                str(key): str(value)
                for key, value in raw.items()
                if str(key) and _is_chat_url(str(value))
            }
        except Exception as exc:
            log.warning("忽略无法读取的 ChatGPT 网页会话映射 %s：%s", self._conversation_file, exc)
            return {}

    def _load_titles(self) -> dict[str, str]:
        if not self._title_file.is_file():
            return {}
        try:
            raw = json.loads(self._title_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("顶层不是 JSON 对象")
            return {
                str(key): str(value).strip()[:80]
                for key, value in raw.items()
                if str(key) and str(value).strip()
            }
        except Exception as exc:
            log.warning("忽略无法读取的 ChatGPT 对话标题映射 %s：%s", self._title_file, exc)
            return {}

    def _load_history(self) -> dict[str, list[dict[str, Any]]]:
        if not self._history_file.is_file():
            return {}
        try:
            raw = json.loads(self._history_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("顶层不是 JSON 对象")
            result: dict[str, list[dict[str, Any]]] = {}
            for raw_key, raw_records in raw.items():
                key = str(raw_key)
                if not key or not isinstance(raw_records, list):
                    continue
                records: list[dict[str, Any]] = []
                seen_urls: set[str] = set()
                for raw_record in raw_records:
                    if not isinstance(raw_record, dict):
                        continue
                    url = str(raw_record.get("url") or "")
                    if not _is_chat_url(url) or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    records.append({
                        "url": url,
                        "title": str(raw_record.get("title") or "")[:80],
                        "updated_at": float(raw_record.get("updated_at") or 0),
                        "archived": bool(raw_record.get("archived", False)),
                    })
                records.sort(
                    key=lambda record: float(record.get("updated_at") or 0),
                    reverse=True,
                )
                if records:
                    result[key] = records[:50]
            return result
        except Exception as exc:
            log.warning("忽略无法读取的 ChatGPT 对话历史 %s：%s", self._history_file, exc)
            return {}

    def _seed_history_from_current(self) -> None:
        changed = False
        timestamp = time.time()
        for session_key, url in self._conversations.items():
            records = self._history.setdefault(session_key, [])
            if any(str(record.get("url") or "") == url for record in records):
                continue
            records.insert(0, {
                "url": url,
                "title": self._titles.get(session_key, ""),
                "updated_at": timestamp,
                "archived": False,
            })
            changed = True
        if changed:
            self._save_history(self._history)

    def _remember_conversation(
        self,
        session_key: str,
        url: str,
        title: str | None,
        *,
        archived: bool | None = None,
    ) -> None:
        if not _is_chat_url(url):
            return
        with self._lock:
            records = [dict(record) for record in self._history.get(session_key, [])]
            previous = next(
                (record for record in records if str(record.get("url") or "") == url),
                None,
            )
            records = [
                record
                for record in records
                if str(record.get("url") or "") != url
            ]
            effective_title = (
                str(title or "").strip()[:80]
                or str((previous or {}).get("title") or "")[:80]
            )
            effective_archived = (
                bool((previous or {}).get("archived", False))
                if archived is None
                else archived
            )
            records.insert(0, {
                "url": url,
                "title": effective_title,
                "updated_at": time.time(),
                "archived": effective_archived,
            })
            self._history[session_key] = records[:50]
            snapshot = {
                key: [dict(record) for record in values]
                for key, values in self._history.items()
            }
        self._save_history(snapshot)

    def _save_history(self, history: dict[str, list[dict[str, Any]]]) -> None:
        temporary = self._history_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(history, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._history_file)

    def _set_conversation(
        self,
        session_key: str,
        url: str,
        title: str | None = None,
    ) -> None:
        if not _is_chat_url(url):
            raise ValueError(f"无效的 ChatGPT 对话地址：{url}")
        with self._lock:
            self._conversations[session_key] = url
            snapshot = dict(self._conversations)
        self._save_conversations(snapshot)
        self._remember_conversation(session_key, url, title, archived=False)

    def _remove_conversation(self, session_key: str) -> None:
        with self._lock:
            self._conversations.pop(session_key, None)
            snapshot = dict(self._conversations)
        self._save_conversations(snapshot)

    def _save_conversations(self, conversations: dict[str, str]) -> None:
        temporary = self._conversation_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(conversations, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._conversation_file)

    def _set_title(self, session_key: str, title: str) -> None:
        with self._lock:
            self._titles[session_key] = title
            url = self._conversations.get(session_key)
            snapshot = dict(self._titles)
        self._save_titles(snapshot)
        if url:
            self._remember_conversation(session_key, url, title)

    def _remove_title(self, session_key: str) -> None:
        with self._lock:
            self._titles.pop(session_key, None)
            snapshot = dict(self._titles)
        self._save_titles(snapshot)

    def _save_titles(self, titles: dict[str, str]) -> None:
        temporary = self._title_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(titles, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._title_file)

    def _stopped(self) -> bool:
        with self._lock:
            return self._state.stop_requested

    def _finish(self) -> None:
        with self._lock:
            self._state.active = False
            self._state.stop_requested = False

    def stop(self) -> str:
        with self._lock:
            if not self._state.active:
                return "当前没有执行中的 ChatGPT 请求"
            self._state.stop_requested = True
        return "正在停止 ChatGPT 请求"

    def restart(self) -> tuple[bool, str]:
        with self._lock:
            if self._state.active:
                return False, "ChatGPT 请求正在执行，请先发送 @停止"
        self.close()
        with self._lock:
            self._closing = False
        try:
            self.start()
        except RuntimeError as exc:
            return False, f"ChatGPT 浏览器重启失败：{exc}"
        return True, "ChatGPT 隐藏浏览器已重启"

    def close(self, timeout_seconds: float = 10) -> None:
        """Stop pending work and close the dedicated browser process."""
        with self._lock:
            self._closing = True
            if self._state.active:
                self._state.stop_requested = True
            worker = self._worker
        if worker is None or not worker.is_alive():
            return
        self._jobs.put(None)
        worker.join(timeout_seconds)
        if worker.is_alive():
            log.warning("等待 ChatGPT 专用浏览器关闭超时")

    def status(self) -> str:
        with self._lock:
            state = self._state
            if not state.active:
                return "ChatGPT Plus 网页当前空闲"
            elapsed = int(time.time() - (state.started_at or time.time()))
            return f"正在执行 ChatGPT {state.operation}，已运行 {elapsed} 秒"

    def drain_events(self) -> list[ChatEvent]:
        result: list[ChatEvent] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result


def has_chatgpt_plus_login(
    profile_dir: Path,
    browser_channel: str,
    proxy_server: str | None,
) -> bool:
    """Check the dedicated profile without opening a visible browser window."""
    if not profile_dir.is_dir() or not any(profile_dir.iterdir()):
        return False

    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            launch_options, hide_native_window = _persistent_context_options(
                profile_dir,
                browser_channel,
                proxy_server,
                hide_window=True,
            )
            context = playwright.chromium.launch_persistent_context(**launch_options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if hide_native_window:
                    _hide_native_browser_window(page)
                PlaywrightChatSession._navigate(page, CHATGPT_HOME, 60)
                return _wait_for_account_state(page, timeout_ms=10_000).plus
            finally:
                context.close()
    except Exception:
        log.warning("无法静默确认 ChatGPT Plus 登录状态", exc_info=True)
        return False


def login_chatgpt(
    profile_dir: Path,
    browser_channel: str,
    proxy_server: str | None,
) -> None:
    """Open the dedicated profile visibly so the owner can complete login."""
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            launch_options, _ = _persistent_context_options(
                profile_dir,
                browser_channel,
                proxy_server,
                hide_window=False,
            )
            context = playwright.chromium.launch_persistent_context(**launch_options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                PlaywrightChatSession._navigate(page, CHATGPT_HOME, 60)
                account = _wait_for_account_state(page, timeout_ms=5_000)
                if account.plus:
                    print("已检测到保存的 ChatGPT Plus 登录状态，无需重新登录。")
                    return
                if account.logged_in:
                    print("当前已登录，但未检测到 Plus 订阅；请切换到 Plus 账号。")
                else:
                    print("请在打开的浏览器中登录你的 ChatGPT Plus 账号。")
                input("登录完成并看到 ChatGPT 输入框后，回到这里按 Enter 保存登录状态：")
                account = _wait_for_account_state(page, timeout_ms=10_000)
                if not account.logged_in:
                    raise RuntimeError("未检测到已登录的 ChatGPT 账号")
                if not account.plus:
                    raise RuntimeError("已登录，但未检测到 ChatGPT Plus 订阅")
                composer = page.locator(PROMPT_SELECTOR).first
                if not composer.is_visible(timeout=5_000):
                    raise RuntimeError("未检测到 ChatGPT 输入框，登录可能尚未完成")
                print("ChatGPT Plus 登录状态已保存。")
            finally:
                context.close()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"ChatGPT Plus 登录失败：{exc}") from exc


def _is_chat_url(value: str | None) -> bool:
    return bool(value and CHAT_URL.match(value))
