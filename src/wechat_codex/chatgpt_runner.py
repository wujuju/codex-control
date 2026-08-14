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
ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
ASSISTANT_FALLBACK_SELECTOR = 'article[data-turn="assistant"]'
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
    ) -> tuple[str, str]: ...


SessionFactory = Callable[[Path, str, bool, str | None], BrowserSession]


@dataclass(frozen=True)
class ChatEvent:
    text: str
    session_key: str | None = None


@dataclass
class ChatState:
    active: bool = False
    session_key: str | None = None
    started_at: float | None = None
    stop_requested: bool = False


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

    def __enter__(self) -> PlaywrightChatSession:
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            launch_options: dict[str, Any] = {
                "user_data_dir": str(self.profile_dir),
                "channel": self.channel,
                "headless": self.headless,
                "viewport": {"width": 1280, "height": 900},
            }
            if self.proxy_server:
                launch_options["proxy"] = {"server": self.proxy_server}
            self._context = self._playwright.chromium.launch_persistent_context(
                **launch_options
            )
            self._context.set_default_timeout(15_000)
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
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

        assistant_messages = page.locator(
            f"{ASSISTANT_SELECTOR}, {ASSISTANT_FALLBACK_SELECTOR}"
        )
        previous_count = assistant_messages.count()
        composer.fill(prompt)
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
        if conversation_title:
            try:
                self._rename_conversation(page, conversation_url, conversation_title)
            except Exception as exc:
                log.warning(
                    "ChatGPT 对话已创建并可继续使用，但标题更新失败（%s）：%s",
                    conversation_title,
                    exc,
                )
        return text, conversation_url

    @staticmethod
    def _rename_conversation(page: Any, conversation_url: str, title: str) -> None:
        """Rename the current web conversation through ChatGPT's visible UI."""
        from urllib.parse import urlparse

        cleaned = " ".join(title.split()).strip()[:80]
        if not cleaned:
            return
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
        try:
            current_text = " ".join(anchor.inner_text(timeout=1_000).split())
        except Exception:
            current_text = ""
        if current_text == cleaned:
            return

        anchor.hover()
        row = anchor.locator(
            "xpath=ancestor::*[self::li or @data-testid][1]"
        )
        if row.count() == 0:
            row = anchor.locator("xpath=..")
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
                        if (
                            not generating
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
        self._jobs: queue.Queue[_ChatJob | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_ready = threading.Event()
        self._startup_error: Exception | None = None
        self._closing = False
        self._conversation_file = runtime_dir / "chatgpt_web_conversations.json"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._conversations = self._load_conversations()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state.active

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
    ) -> tuple[bool, str]:
        try:
            self.start()
        except RuntimeError as exc:
            return False, str(exc)

        with self._lock:
            if self._state.active:
                return False, "已有 ChatGPT 请求在执行，请发送“状态”或“停止”"
            conversation_url = self._conversations.get(session_key)
            self._state = ChatState(
                active=True,
                session_key=session_key,
                started_at=time.time(),
            )

        self._jobs.put(
            _ChatJob(
                session_key=session_key,
                prompt=prompt,
                conversation_url=conversation_url,
                conversation_title=conversation_title,
            )
        )
        return True, "已开始 ChatGPT Plus 网页请求"

    def begin_reset(self, session_key: str) -> tuple[bool, str]:
        with self._lock:
            if self._state.active:
                return False, "已有 ChatGPT 请求在执行，请发送“状态”或“停止”"
            if session_key not in self._conversations:
                return False, "当前已经是新的 ChatGPT 对话"
        self._remove_conversation(session_key)
        self.events.put(
            ChatEvent(
                "已切换到新的 ChatGPT 对话；旧对话仍保留在你的 ChatGPT 历史中",
                session_key,
            )
        )
        return True, "已切换到新的 ChatGPT 对话"

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
            text, new_url = session.ask(
                job.conversation_url,
                job.prompt,
                job.conversation_title,
                self.timeout_seconds,
                self._stopped,
            )

            if self._stopped():
                self.events.put(ChatEvent("ChatGPT 请求已停止", job.session_key))
                return
            self._set_conversation(job.session_key, new_url)
            self.events.put(ChatEvent(text, job.session_key))
        except ChatStopped:
            self.events.put(ChatEvent("ChatGPT 请求已停止", job.session_key))
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            self.events.put(ChatEvent(
                f"ChatGPT Plus 网页请求失败：{detail[-1000:]}", job.session_key
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
            return f"正在执行 ChatGPT Plus 网页聊天，已运行 {elapsed} 秒"

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
            launch_options: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "channel": browser_channel,
                "headless": True,
                "viewport": {"width": 1280, "height": 900},
            }
            if proxy_server:
                launch_options["proxy"] = {"server": proxy_server}
            context = playwright.chromium.launch_persistent_context(**launch_options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
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
            launch_options: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "channel": browser_channel,
                "headless": False,
                "viewport": {"width": 1280, "height": 900},
            }
            if proxy_server:
                launch_options["proxy"] = {"server": proxy_server}
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
