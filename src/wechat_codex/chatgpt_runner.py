from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


log = logging.getLogger(__name__)


CHATGPT_HOME = "https://chatgpt.com/"
CHAT_URL = re.compile(r"^https://chatgpt\.com/(?:g/[^/]+/)?c/", re.IGNORECASE)
PROMPT_SELECTOR = '[data-testid="prompt-textarea"], #prompt-textarea'
SEND_SELECTOR = (
    'button[data-testid="send-button"], '
    'button[data-testid="composer-submit-button"]'
)
STOP_SELECTOR = (
    'button[data-testid="stop-button"], '
    'button[data-testid="composer-stop-button"]'
)
ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
ASSISTANT_FALLBACK_SELECTOR = 'article[data-turn="assistant"]'
CHAT_PROMPT_PREFIX = (
    "这是从我的个人微信转发到 ChatGPT 的消息。请使用中文，回答适合微信阅读，"
    "简洁、直接、准确。需要操作本地项目时，请提示我使用“干活：任务”命令。\n\n"
    "微信消息：\n"
)


class ChatStopped(RuntimeError):
    pass


class BrowserSession(Protocol):
    def __enter__(self) -> BrowserSession: ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...

    def ask(
        self,
        conversation_url: str | None,
        prompt: str,
        timeout_seconds: int,
        stopped: Callable[[], bool],
    ) -> tuple[str, str]: ...


SessionFactory = Callable[[Path, str, bool], BrowserSession]


@dataclass(frozen=True)
class ChatEvent:
    text: str


@dataclass
class ChatState:
    active: bool = False
    session_key: str | None = None
    started_at: float | None = None
    stop_requested: bool = False


class PlaywrightChatSession:
    """One short-lived browser process backed by a persistent Plus profile."""

    def __init__(self, profile_dir: Path, channel: str, headless: bool) -> None:
        self.profile_dir = profile_dir
        self.channel = channel
        self.headless = headless
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    def __enter__(self) -> PlaywrightChatSession:
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel=self.channel,
                headless=self.headless,
                viewport={"width": 1280, "height": 900},
            )
            self._context.set_default_timeout(15_000)
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
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
        timeout_seconds: int,
        stopped: Callable[[], bool],
    ) -> tuple[str, str]:
        page = self._page
        if page is None:
            raise RuntimeError("ChatGPT 浏览器尚未启动")

        target = conversation_url if _is_chat_url(conversation_url) else CHATGPT_HOME
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

        assistant_messages = page.locator(ASSISTANT_SELECTOR)
        if assistant_messages.count() == 0:
            assistant_messages = page.locator(ASSISTANT_FALLBACK_SELECTOR)
        previous_count = assistant_messages.count()
        composer.fill(CHAT_PROMPT_PREFIX + prompt)
        send_button = page.locator(SEND_SELECTOR).first
        send_button.wait_for(state="visible", timeout=5_000)
        send_button.click()

        text = self._wait_for_reply(
            page,
            assistant_messages,
            previous_count,
            timeout_seconds,
            stopped,
        )
        conversation_url = self._wait_for_conversation_url(page)
        return text, conversation_url

    @staticmethod
    def _navigate(page: Any, url: str, timeout_seconds: int) -> None:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=min(timeout_seconds, 45) * 1000,
        )

    @staticmethod
    def _wait_for_composer(page: Any) -> Any:
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
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        last_text = ""
        unchanged_since: float | None = None

        while time.monotonic() < deadline:
            if stopped():
                raise ChatStopped("ChatGPT 请求已停止")

            count = assistant_messages.count()
            if count > previous_count:
                try:
                    current = assistant_messages.nth(count - 1).inner_text(
                        timeout=2_000
                    ).strip()
                except Exception:
                    current = ""
                if current:
                    if current != last_text:
                        last_text = current
                        unchanged_since = time.monotonic()
                    elif unchanged_since is not None:
                        generating = page.locator(STOP_SELECTOR).first.is_visible(
                            timeout=500
                        )
                        ready = page.locator(SEND_SELECTOR).first.is_visible(timeout=500)
                        if (
                            not generating
                            and ready
                            and time.monotonic() - unchanged_since >= 1.5
                        ):
                            return current

            page.wait_for_timeout(250)

        if last_text:
            raise TimeoutError("ChatGPT 回复仍在生成，等待超时")
        raise TimeoutError("等待 ChatGPT 回复超时")

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
        timeout_seconds: int,
        runtime_dir: Path,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.browser_channel = browser_channel
        self.headless = headless
        self.timeout_seconds = timeout_seconds
        self.runtime_dir = runtime_dir
        self.profile_dir = runtime_dir / "chatgpt-plus-profile"
        self.events: queue.Queue[ChatEvent] = queue.Queue()
        self._session_factory = session_factory or PlaywrightChatSession
        self._state = ChatState()
        self._lock = threading.RLock()
        self._conversation_file = runtime_dir / "chatgpt_web_conversations.json"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._conversations = self._load_conversations()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state.active

    def begin_chat(self, session_key: str, prompt: str) -> tuple[bool, str]:
        with self._lock:
            if self._state.active:
                return False, "已有 ChatGPT 请求在执行，请发送“状态”或“停止”"
            self._state = ChatState(
                active=True,
                session_key=session_key,
                started_at=time.time(),
            )

        worker = threading.Thread(
            target=self._run_chat,
            args=(session_key, prompt),
            name="chatgpt-plus-web",
            daemon=True,
        )
        worker.start()
        return True, "已开始 ChatGPT Plus 网页请求"

    def begin_reset(self, session_key: str) -> tuple[bool, str]:
        with self._lock:
            if self._state.active:
                return False, "已有 ChatGPT 请求在执行，请发送“状态”或“停止”"
            if session_key not in self._conversations:
                return False, "当前已经是新的 ChatGPT 对话"
        self._remove_conversation(session_key)
        self.events.put(
            ChatEvent("已切换到新的 ChatGPT 对话；旧对话仍保留在你的 ChatGPT 历史中")
        )
        return True, "已切换到新的 ChatGPT 对话"

    def _run_chat(self, session_key: str, prompt: str) -> None:
        try:
            with self._lock:
                conversation_url = self._conversations.get(session_key)
            with self._session_factory(
                self.profile_dir,
                self.browser_channel,
                self.headless,
            ) as session:
                text, new_url = session.ask(
                    conversation_url,
                    prompt,
                    self.timeout_seconds,
                    self._stopped,
                )

            if self._stopped():
                self.events.put(ChatEvent("ChatGPT 请求已停止"))
                return
            self._set_conversation(session_key, new_url)
            self.events.put(ChatEvent(text))
        except ChatStopped:
            self.events.put(ChatEvent("ChatGPT 请求已停止"))
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            self.events.put(ChatEvent(f"ChatGPT Plus 网页请求失败：{detail[-1000:]}"))
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

    def _set_conversation(self, session_key: str, url: str) -> None:
        if not _is_chat_url(url):
            raise ValueError(f"无效的 ChatGPT 对话地址：{url}")
        with self._lock:
            self._conversations[session_key] = url
            snapshot = dict(self._conversations)
        self._save_conversations(snapshot)

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

    def status(self) -> str:
        with self._lock:
            state = self._state
            if not state.active:
                return "ChatGPT Plus 网页当前空闲"
            elapsed = int(time.time() - (state.started_at or time.time()))
            return f"正在执行 ChatGPT Plus 网页聊天，已运行 {elapsed} 秒"

    def drain_events(self) -> list[ChatEvent]:
        result: list[ChatEvent] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result


def login_chatgpt(profile_dir: Path, browser_channel: str) -> None:
    """Open the dedicated profile visibly so the owner can complete login."""
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel=browser_channel,
                headless=False,
                viewport={"width": 1280, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(CHATGPT_HOME, wait_until="domcontentloaded", timeout=60_000)
                print("请在打开的浏览器中登录你的 ChatGPT Plus 账号。")
                input("登录完成并看到 ChatGPT 输入框后，回到这里按 Enter 保存登录状态：")
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
