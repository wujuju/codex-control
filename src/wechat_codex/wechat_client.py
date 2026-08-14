from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Iterable

from .wx_cli_reader import WxCliReader


log = logging.getLogger(__name__)


class WeChatAccessibilityUnavailable(RuntimeError):
    """WeChat is open but has not published its message control tree."""

    def __init__(self, version: str | None = None) -> None:
        version_text = f"（{version}）" if version else ""
        super().__init__(
            f"已找到微信窗口{version_text}，但微信未开放消息控件树。"
            "请先从托盘完全退出微信，按 Win+Ctrl+Enter 打开 Windows 讲述人，"
            "再重新启动并登录微信；程序连接成功后可以关闭讲述人。"
            "这不是 contact 名称或登录状态错误。"
        )
        self.version = version


@dataclass(frozen=True)
class IncomingMessage:
    key: str
    content: str
    sender: str
    attr: str
    conversation: str = ""
    chat_type: str = "friend"


def _message_key(message: Any) -> str:
    # ``hash`` and ``hash_text`` include the rendered message text. WeChat
    # mutates both after a voice message has been transcribed, while ``id``
    # remains tied to the same UI item. Prefer it so one voice cannot be
    # processed twice merely because its transcript appeared.
    for name in ("id", "hash", "hash_text"):
        value = getattr(message, name, None)
        if value not in (None, ""):
            return f"{name}:{value}"
    raw = "|".join(
        str(getattr(message, name, "")) for name in ("attr", "sender", "type", "content")
    )
    return "fallback:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _message_attr(message: Any) -> str:
    attr = str(getattr(message, "attr", "")).lower()
    if attr:
        return attr
    class_name = type(message).__name__.lower()
    if "friend" in class_name:
        return "friend"
    if "self" in class_name:
        return "self"
    return ""


def is_voice_message(message: Any) -> bool:
    message_type = str(getattr(message, "type", "")).lower()
    class_name = type(message).__name__.lower()
    return message_type == "voice" or "voicemessage" in class_name


_VOICE_LABEL = re.compile(
    r"^\s*(?:\[?语音\]?)\s*\d+(?:\.\d+)?\s*(?:[\"”″']\s*)?秒\s*"
)


def extract_voice_transcript(content: str) -> str:
    """Return text appended by WeChat's native auto-transcription setting."""
    match = _VOICE_LABEL.match(content)
    if match is None:
        return ""
    transcript = content[match.end() :].strip()
    if transcript in {"转文字失败", "语音转文字失败"}:
        return ""
    return transcript


def strip_required_group_mention(content: str, bot_name: str) -> str | None:
    """Remove one explicit bot mention, or reject a group message without it."""
    pattern = re.compile(rf"@\s*{re.escape(bot_name)}", re.IGNORECASE)
    match = pattern.search(content)
    if match is None:
        return None
    cleaned = (content[: match.start()] + content[match.end() :]).strip()
    return cleaned.lstrip("\u2005\u2006\u2009 ,，:：")


def normalize_message(message: Any) -> IncomingMessage | None:
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        return None

    attr = _message_attr(message)
    class_name = type(message).__name__.lower()

    message_type = str(getattr(message, "type", "")).lower()
    is_text = message_type in {"text", "quote"} or any(
        token in class_name for token in ("textmessage", "quotemessage")
    )
    if not is_text or attr not in {"friend", "self"}:
        return None

    return IncomingMessage(
        key=_message_key(message),
        content=content.strip(),
        sender=str(getattr(message, "sender", "")),
        attr=attr,
    )


def normalize_voice_message(
    message: Any,
    recognized_text: str,
    *,
    key: str | None = None,
) -> IncomingMessage | None:
    if not is_voice_message(message):
        return None
    attr = _message_attr(message)
    content = recognized_text.strip()
    if attr not in {"friend", "self"} or not content:
        return None
    return IncomingMessage(
        key=key or _message_key(message),
        content=content,
        sender=str(getattr(message, "sender", "")),
        attr=attr,
    )


def split_text(text: str, limit: int) -> list[str]:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return [cleaned]
    chunks: list[str] = []
    remaining = cleaned
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks


class WeChatClient:
    def __init__(
        self,
        contact: str,
        background_mode: bool,
        allow_self_messages: bool,
        voice_recognition: bool,
        voice_retry_count: int,
        response_prefix: str,
        max_reply_chars: int,
        chat_type: str = "friend",
        bot_name: str = "ChatGpt机器人",
        message_source: str = "uia",
        wx_cli_path: str | None = None,
        wx_cli_username: str | None = None,
        wx_cli_timeout_seconds: float = 30.0,
    ) -> None:
        self.contact = contact
        self.background_mode = background_mode
        self.allow_self_messages = allow_self_messages
        self.voice_recognition = voice_recognition
        self.voice_retry_count = voice_retry_count
        self.response_prefix = response_prefix
        self.max_reply_chars = max_reply_chars
        self.chat_type = chat_type
        self.bot_name = bot_name
        self.message_source = message_source
        self._wx_cli_reader = (
            WxCliReader(
                contact=contact,
                chat_type=chat_type,
                executable=wx_cli_path,
                username=wx_cli_username,
                timeout_seconds=wx_cli_timeout_seconds,
            )
            if message_source == "wx_cli"
            else None
        )
        self._wx: Any = None
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()
        self._seen_limit = 1000
        self._voice_failures: dict[str, tuple[int, float]] = {}
        self._invoke_send_supported: bool | None = None
        self._root: Any = None
        self._chatbox: Any = None
        self._active_chat_type = "friend"
        self._avatar_recovery_attempted = False
        self._session_watch_initialized = False
        self._target_session_control: Any = None
        self._last_session_lookup_at = 0.0
        self._session_lookup_interval_seconds = 10.0
        self._target_session_signature: tuple[tuple[str, str, str], ...] | None = None
        self._target_session_unread_count: int | None = None
        self._pending_session_message_count: int | None = None

    def connect(self) -> None:
        if self._wx_cli_reader is not None:
            self._wx_cli_reader.connect()
            return
        self._connect_uia()

    def _connect_uia(self) -> None:
        # wxauto4 configures the process-wide root logger during import and
        # clears handlers installed by the GUI/CLI. Preserve the host
        # application's logging destination across that import.
        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        original_level = root_logger.level
        try:
            try:
                import psutil
                import win32gui
                import win32process
                from wxauto4.uia import uiautomation as uia
                from wxauto4.ui.chatbox import ChatBox
            finally:
                if original_handlers:
                    root_logger.handlers.clear()
                    root_logger.handlers.extend(original_handlers)
                    root_logger.setLevel(original_level)
        except ImportError as exc:  # pragma: no cover - depends on Windows package install
            raise RuntimeError("未安装 wxauto4，请运行 pip install -e .") from exc

        failure: Exception | None = None
        handles: list[int] = []
        wechat_version: str | None = None
        try:
            processes = [
                process.info
                for process in psutil.process_iter(["name", "pid", "exe"])
                if (process.info.get("name") or "").lower() == "weixin.exe"
            ]
            pids = {process["pid"] for process in processes}
            wechat_version = self._detect_wechat_version(
                process.get("exe") for process in processes
            )

            def collect(hwnd: int, _: Any) -> None:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid not in pids:
                    return
                if "QWindowIcon" in win32gui.GetClassName(hwnd):
                    handles.append(hwnd)

            win32gui.EnumWindows(collect, None)
            self._root = self._find_wechat_root(handles, uia)
            if self._root is None:
                if handles:
                    raise WeChatAccessibilityUnavailable(wechat_version)
                raise LookupError("未找到已登录的微信主窗口")

            self._initialize_chatbox(uia, ChatBox, win32gui)
            self._avatar_recovery_attempted = False
            return
        except Exception as exc:
            failure = exc

        if (
            handles
            and not self._avatar_recovery_attempted
            and self._is_event_subscriber_error(failure)
        ):
            self._avatar_recovery_attempted = True
            log.warning("微信 UIA 尚未就绪，自动点击左上角头像后重试连接")
            try:
                recovery_hwnd = self._select_recovery_hwnd(handles, win32gui)
                self._prime_wechat_avatar(win32gui, recovery_hwnd)
                self._root = self._find_wechat_root(handles, uia)
                if self._root is None:
                    raise LookupError("点击头像后仍未找到微信主窗口")
                self._initialize_chatbox(uia, ChatBox, win32gui)
                self._avatar_recovery_attempted = False
                return
            except Exception as recovery_error:
                failure = recovery_error

        assert failure is not None
        if isinstance(failure, WeChatAccessibilityUnavailable):
            raise failure
        raise RuntimeError(
            "无法连接 PC 微信。请确认微信 4.x 已登录、窗口未退出，并检查 contact 名称。"
            f"原始错误：{type(failure).__name__}: {failure}"
        ) from failure

    @staticmethod
    def _detect_wechat_version(executables: Iterable[str | None]) -> str | None:
        """Read the first available Weixin.exe product version."""
        try:
            import win32api
        except ImportError:  # pragma: no cover - pywin32 is required on Windows
            return None

        checked: set[str] = set()
        for executable in executables:
            if not executable or executable in checked:
                continue
            checked.add(executable)
            try:
                info = win32api.GetFileVersionInfo(executable, "\\")
                ms = int(info["ProductVersionMS"])
                ls = int(info["ProductVersionLS"])
                return ".".join(
                    str(part)
                    for part in (
                        ms >> 16,
                        ms & 0xFFFF,
                        ls >> 16,
                        ls & 0xFFFF,
                    )
                )
            except Exception:
                log.debug("读取微信版本失败：%s", executable, exc_info=True)
        return None

    def _initialize_chatbox(self, uia: Any, ChatBox: Any, win32gui: Any) -> None:
        """Initialize passive session watching without forcing a chat switch."""
        self._invoke_send_supported = None
        if self.background_mode:
            self._send_window_to_background(win32gui)
        session = self._target_session(uia, force_lookup=True)
        self._target_session_signature = self._session_signature(session)
        self._target_session_unread_count = self._session_unread_count(session)
        self._session_watch_initialized = True
        if not self._is_target_contact(self._current_contact(uia)):
            self._chatbox = None
            self._wx = None
            log.info("目标会话 %s 当前未打开，等待会话列表出现新消息", self.contact)
            return
        self._bind_current_chatbox(uia, ChatBox)

    def _bind_current_chatbox(self, uia: Any, ChatBox: Any) -> None:
        page = self._find_control(uia, automation_id="chat_message_page")
        if page is None:
            raise LookupError("未找到聊天页面")

        self._active_chat_type = self._resolve_chat_type(uia)
        parent = _NativeParent(self._root, self.contact, self._active_chat_type)
        self._chatbox = ChatBox(page, parent)
        self._wx = self._chatbox

    @staticmethod
    def _find_wechat_root(handles: Iterable[int], uia: Any) -> Any:
        for hwnd in handles:
            candidate = uia.ControlFromHandle(hwnd)
            if candidate and candidate.ClassName == "mmui::MainWindow":
                return candidate
        return None

    @staticmethod
    def _select_recovery_hwnd(handles: Iterable[int], win32gui: Any) -> int:
        """Prefer the largest visible WeChat top-level window."""

        def visible_client_area(hwnd: int) -> int:
            if not win32gui.IsWindowVisible(hwnd):
                return -1
            try:
                left, top, right, bottom = win32gui.GetClientRect(hwnd)
            except Exception:
                return -1
            return max(0, right - left) * max(0, bottom - top)

        candidates = list(handles)
        if not candidates:
            raise LookupError("未找到可用于头像恢复的微信窗口")
        return int(max(candidates, key=visible_client_area))

    @staticmethod
    def _is_event_subscriber_error(exc: BaseException | None) -> bool:
        """Match COM CONNECT_E_NOCONNECTION, including wrapped exceptions."""
        seen: set[int] = set()
        current = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            hresult = getattr(current, "hresult", None)
            if hresult == -2147220991:
                return True
            args = getattr(current, "args", ())
            if args and args[0] == -2147220991:
                return True
            if "事件无法调用任何订户" in str(current):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _prime_wechat_avatar(self, win32gui: Any, hwnd: int) -> None:
        """Perform the real click WeChat 4.x needs to publish its UIA tree."""
        import win32api
        import win32con

        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            raise LookupError("微信主窗口不可见，无法自动点击头像")

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        if width < 120 or height < 180:
            raise LookupError("微信主窗口尺寸异常，无法定位头像")

        previous_foreground = win32gui.GetForegroundWindow()
        previous_cursor = win32api.GetCursorPos()
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0, flags)
            win32gui.SetForegroundWindow(hwnd)
            if win32gui.GetForegroundWindow() != hwnd:
                raise RuntimeError("无法临时激活微信窗口，未执行头像点击")

            # These controls are positioned relative to the whole WeChat
            # window, not its client area (which begins below the title bar).
            avatar_x, avatar_y = left + 30, top + 62
            chat_tab_x, chat_tab_y = left + 30, top + 114
            win32api.SetCursorPos((avatar_x, avatar_y))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.4)
            # Return to the chat tab so the profile popover does not obstruct
            # subsequent background reads.
            win32api.SetCursorPos((chat_tab_x, chat_tab_y))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.3)
        finally:
            try:
                win32api.SetCursorPos(previous_cursor)
            except Exception:
                log.debug("恢复鼠标位置失败", exc_info=True)
            if previous_foreground and previous_foreground != hwnd:
                try:
                    win32gui.SetForegroundWindow(previous_foreground)
                except Exception as exc:
                    log.warning("头像恢复流程无法还原前台窗口：%s", exc)
            if self.background_mode:
                try:
                    self._send_window_to_background(win32gui, hwnd)
                except Exception as exc:
                    log.warning("头像恢复流程无法将微信放回后台：%s", exc)

    def _send_window_to_background(self, win32gui: Any, hwnd: int | None = None) -> None:
        """Keep WeChat behind other windows without activating or hiding it."""
        import win32con

        if hwnd is None:
            if self._root is None:
                raise LookupError("微信主窗口尚未初始化")
            hwnd = int(self._root.NativeWindowHandle)
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            raise LookupError(
                "微信主窗口已最小化或关闭到托盘；微信 4.1.12 在此状态下不提供消息控件，"
                "请打开主窗口，程序不会抢占焦点"
            )
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_BOTTOM,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE
            | win32con.SWP_NOSIZE
            | win32con.SWP_NOACTIVATE
            | win32con.SWP_NOOWNERZORDER,
        )

    def _walk_roots(self, uia: Any) -> Iterable[Any]:
        if self._root is not None:
            yield self._root
        try:
            import win32gui
            import win32process

            pid = self._root.ProcessId if self._root is not None else None
            handles: list[int] = []

            def collect(hwnd: int, _: Any) -> None:
                _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
                if window_pid == pid and win32gui.IsWindowVisible(hwnd):
                    handles.append(hwnd)

            win32gui.EnumWindows(collect, None)
            for hwnd in handles:
                candidate = uia.ControlFromHandle(hwnd)
                if candidate and candidate != self._root:
                    yield candidate
        except Exception:
            return

    def _find_control(
        self,
        uia: Any,
        *,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        prefix_name: bool = False,
    ) -> Any:
        for root in self._walk_roots(uia):
            for control, _depth in uia.WalkControl(root, includeTop=True, maxDepth=35):
                if automation_id is not None and control.AutomationId != automation_id:
                    continue
                if control_type is not None and control.ControlTypeName != control_type:
                    continue
                if name is not None:
                    control_name = control.Name or ""
                    if prefix_name:
                        if control_name.splitlines()[0].strip() != name:
                            continue
                    elif control_name != name:
                        continue
                return control
        return None

    def _current_contact(self, uia: Any) -> str:
        suffix = "current_chat_name_label"
        for control, _depth in uia.WalkControl(self._root, includeTop=True, maxDepth=35):
            if (control.AutomationId or "").endswith(suffix):
                return (control.Name or "").strip()
        return ""

    def _is_target_contact(self, current: str) -> bool:
        current = current.strip()
        if current == self.contact:
            return True
        if self.chat_type in {"group", "auto"}:
            without_member_count = re.sub(
                r"\s*[（(]\s*\d+\s*[)）]\s*$",
                "",
                current,
            )
            return without_member_count == self.contact
        return False

    def _find_session_item(self, uia: Any) -> Any:
        return self._find_control(
            uia,
            automation_id=f"session_item_{self.contact}",
        )

    def _target_session(self, uia: Any, *, force_lookup: bool = False) -> Any:
        """Reuse the UIA session item instead of searching the tree every poll."""
        cached = self._target_session_control
        if cached is not None and self._session_signature(cached) is not None:
            return cached
        self._target_session_control = None

        now = time.monotonic()
        if (
            not force_lookup
            and now - self._last_session_lookup_at
            < self._session_lookup_interval_seconds
        ):
            return None
        self._last_session_lookup_at = now
        self._target_session_control = self._find_session_item(uia)
        return self._target_session_control

    @staticmethod
    def _session_signature(control: Any) -> tuple[tuple[str, str, str], ...] | None:
        """Capture preview/time/unread child changes from a session list item."""
        if control is None:
            return None
        result: list[tuple[str, str, str]] = []
        pending: list[tuple[Any, int]] = [(control, 0)]
        while pending:
            current, depth = pending.pop(0)
            try:
                result.append(
                    (
                        str(getattr(current, "Name", "") or ""),
                        str(getattr(current, "AutomationId", "") or ""),
                        str(getattr(current, "ClassName", "") or ""),
                    )
                )
                if depth < 5:
                    pending.extend((child, depth + 1) for child in current.GetChildren())
            except Exception:
                continue
        return tuple(result) if result else None

    @staticmethod
    def _session_unread_count(control: Any) -> int | None:
        """Read a numeric unread badge when WeChat exposes one through UIA."""
        if control is None:
            return None
        counts: list[int] = []
        pending: list[tuple[Any, int]] = [(control, 0)]
        while pending:
            current, depth = pending.pop(0)
            try:
                name = str(getattr(current, "Name", "") or "").strip()
                class_name = str(getattr(current, "ClassName", "") or "").lower()
                match = re.search(r"(\d+)\s*条?新消息", name)
                if match:
                    counts.append(int(match.group(1)))
                elif name.isdigit() and current is not control and (
                    "badge" in class_name or "unread" in class_name
                ):
                    counts.append(int(name))
                if depth < 5:
                    pending.extend((child, depth + 1) for child in current.GetChildren())
            except Exception:
                continue
        return max(counts) if counts else None

    def _resolve_chat_type(self, uia: Any) -> str:
        if self.chat_type != "auto":
            return self.chat_type
        header = self._current_contact(uia)
        if re.search(r"[（(]\s*\d+\s*[)）]\s*$", header):
            return "group"
        return "friend"

    def _ensure_contact(self, uia: Any) -> None:
        if self._is_target_contact(self._current_contact(uia)):
            return

        session = self._target_session(uia, force_lookup=True)
        if session is not None:
            self._select_without_focus(session)
            time.sleep(0.5)
            if self._is_target_contact(self._current_contact(uia)):
                return

        search = self._find_control(
            uia,
            name="搜索",
            control_type="EditControl",
        )
        if search is None:
            raise LookupError(f"找不到联系人 {self.contact!r}，也未找到微信搜索框")
        value = search.GetValuePattern()
        if value is None:
            raise LookupError("微信搜索框不可写")
        value.SetValue(self.contact)
        try:
            time.sleep(1)
            result = self._find_control(
                uia,
                name=self.contact,
                control_type="ListItemControl",
                prefix_name=True,
            )
            if result is not None:
                invoke = result.GetInvokePattern()
                if invoke is not None:
                    invoke.Invoke()
                else:
                    self._select_without_focus(result)
            else:
                raise LookupError(f"微信搜索结果中没有联系人 {self.contact!r}")
            time.sleep(0.8)
        finally:
            try:
                value.SetValue("")
            except Exception:
                log.debug("清理微信搜索框失败", exc_info=True)
        if not self._is_target_contact(self._current_contact(uia)):
            raise LookupError(f"未能切换到会话 {self.contact!r}")

    @staticmethod
    def _select_without_focus(control: Any) -> None:
        selection = control.GetSelectionItemPattern()
        if selection is not None:
            selection.Select()
            return
        invoke = control.GetInvokePattern()
        if invoke is not None:
            invoke.Invoke()
            return
        raise LookupError("目标微信控件不支持后台选择")

    def _refresh_target(self) -> None:
        from wxauto4.uia import uiautomation as uia
        from wxauto4.ui.chatbox import ChatBox

        self._ensure_contact(uia)
        self._bind_current_chatbox(uia, ChatBox)
        session = self._target_session(uia, force_lookup=True)
        self._target_session_signature = self._session_signature(session)
        self._target_session_unread_count = self._session_unread_count(session)
        self._session_watch_initialized = True

    def _activate_target_for_new_message(self, uia: Any, ChatBox: Any) -> bool:
        """Switch only when the target session's visible metadata changed."""
        session = self._target_session(uia)
        signature = self._session_signature(session)
        unread_count = self._session_unread_count(session)
        if not self._session_watch_initialized:
            self._target_session_signature = signature
            self._target_session_unread_count = unread_count
            self._session_watch_initialized = True
            return False
        if session is None or signature == self._target_session_signature:
            return False

        log.info("检测到目标会话 %s 更新，切换后读取新消息", self.contact)
        if unread_count is not None and self._target_session_unread_count is not None:
            self._pending_session_message_count = max(
                1, unread_count - self._target_session_unread_count
            )
        elif unread_count is not None:
            self._pending_session_message_count = max(1, unread_count)
        else:
            self._pending_session_message_count = 1
        if not self._is_target_contact(self._current_contact(uia)):
            self._select_without_focus(session)
            time.sleep(0.5)
            if not self._is_target_contact(self._current_contact(uia)):
                raise LookupError(f"检测到新消息，但未能切换到会话 {self.contact!r}")
        if self._wx is None:
            self._bind_current_chatbox(uia, ChatBox)
        self._target_session_signature = self._session_signature(session)
        self._target_session_unread_count = self._session_unread_count(session)
        return True

    def _remember(self, key: str) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > self._seen_limit:
            self._seen.discard(self._seen_order.popleft())

    def _all_messages(self) -> Iterable[Any]:
        from wxauto4.uia import uiautomation as uia
        from wxauto4.ui.chatbox import ChatBox

        if not self._activate_target_for_new_message(uia, ChatBox):
            return []
        if not self.background_mode:
            messages = list(self._chatbox.get_msgs())
        else:
            messages = self._native_messages()
        if self._pending_session_message_count is not None:
            new_count = self._pending_session_message_count
            for raw in messages[: max(0, len(messages) - new_count)]:
                self._remember(_message_key(raw))
            self._pending_session_message_count = None
        session = self._target_session(uia)
        self._target_session_signature = self._session_signature(session)
        self._target_session_unread_count = self._session_unread_count(session)
        return messages

    def _native_messages(self) -> list[Any]:
        """Read visible message items without wxauto's focus-stealing scan.

        wxauto4's ``ChatBox.get_msgs()`` activates WeChat on every call under
        WeChat 4.1.12. Constructing its individual message wrappers from the
        already exposed UIA list items does not, and still gives us stable
        message IDs plus the native voice transcript.
        """
        from wxauto4.msgs.friend import (
            FriendQuoteMessage,
            FriendTextMessage,
            FriendVoiceMessage,
        )
        from wxauto4.uia import uiautomation as uia

        message_list = self._find_control(uia, automation_id="chat_message_list")
        if message_list is None:
            raise LookupError("找不到微信消息列表；请保持微信主窗口打开且不要最小化")

        wrappers = {
            "mmui::ChatTextItemView": FriendTextMessage,
            "mmui::ChatQuoteItemView": FriendQuoteMessage,
            "mmui::ChatVoiceItemView": FriendVoiceMessage,
        }
        messages: list[Any] = []
        for control in message_list.GetChildren():
            wrapper = wrappers.get(control.ClassName)
            if wrapper is None:
                continue
            try:
                messages.append(wrapper(control, self._chatbox))
            except Exception as exc:
                log.debug("跳过无法解析的微信消息控件 %s：%s", control.ClassName, exc)
        return messages

    def baseline(self) -> int:
        if self._wx_cli_reader is not None:
            return self._wx_cli_reader.baseline()
        count = 0
        for raw in self._all_messages():
            message = normalize_message(raw)
            if message or is_voice_message(raw):
                self._remember(_message_key(raw))
                count += 1
        return count

    def poll(self) -> list[IncomingMessage]:
        if self._wx_cli_reader is not None:
            return self._poll_wx_cli()
        incoming: list[IncomingMessage] = []
        for raw in self._all_messages():
            raw_key = _message_key(raw)
            if raw_key in self._seen:
                continue
            message = normalize_message(raw)
            if message is None and is_voice_message(raw):
                message = self._recognize_voice(raw, raw_key)
            if not message:
                continue
            self._remember(message.key)
            self._voice_failures.pop(message.key, None)
            if message.content.startswith(self.response_prefix):
                continue
            if message.attr == "self" and not self.allow_self_messages:
                continue
            sender = message.sender
            content = message.content
            if self._active_chat_type == "group":
                content = strip_required_group_mention(content, self.bot_name)
                if content is None or not content:
                    continue
            elif sender in {"", "friend"}:
                sender = self.contact
            message = replace(
                message,
                content=content,
                sender=sender,
                conversation=self.contact,
                chat_type=self._active_chat_type,
            )
            incoming.append(message)
        return incoming

    def _poll_wx_cli(self) -> list[IncomingMessage]:
        assert self._wx_cli_reader is not None
        incoming: list[IncomingMessage] = []
        for raw in self._wx_cli_reader.poll():
            if raw.key in self._seen:
                continue
            self._remember(raw.key)
            if raw.content.startswith(self.response_prefix):
                continue
            attr = "self" if raw.is_self else "friend"
            if attr == "self" and not self.allow_self_messages:
                continue
            content = raw.content
            sender = raw.sender
            if raw.chat_type == "group":
                content = strip_required_group_mention(content, self.bot_name)
                if content is None or not content:
                    continue
            elif not sender or sender == "friend":
                sender = self.contact
            incoming.append(
                IncomingMessage(
                    key=raw.key,
                    content=content,
                    sender=sender,
                    attr=attr,
                    conversation=raw.conversation,
                    chat_type=raw.chat_type,
                )
            )
        return incoming

    def _recognize_voice(self, raw: Any, key: str) -> IncomingMessage | None:
        attr = _message_attr(raw)
        if attr not in {"friend", "self"}:
            self._remember(key)
            return None
        if attr == "self" and not self.allow_self_messages:
            self._remember(key)
            return None
        if not self.voice_recognition:
            self._remember(key)
            return None

        attempts, retry_after = self._voice_failures.get(key, (0, 0.0))
        if time.monotonic() < retry_after:
            return None
        try:
            recognized = extract_voice_transcript(str(getattr(raw, "content", "")))
            if not recognized and not self.background_mode:
                converter = getattr(raw, "to_text", None)
                if callable(converter):
                    recognized = converter()
            if not isinstance(recognized, str) or not recognized.strip():
                raise RuntimeError("等待微信原生语音转写文本")
            message = normalize_voice_message(raw, recognized, key=key)
            if message:
                log.info("语音识别成功（%s）：%s", message.sender, message.content)
            return message
        except Exception as exc:
            attempts += 1
            if attempts >= self.voice_retry_count:
                self._remember(key)
                self._voice_failures.pop(key, None)
                log.warning("语音识别最终失败：%s", exc)
            else:
                self._voice_failures[key] = (attempts, time.monotonic() + 3.0)
                log.warning(
                    "语音识别失败（%d/%d），稍后重试：%s",
                    attempts,
                    self.voice_retry_count,
                    exc,
                )
            return None

    def send(self, text: str) -> None:
        if self._root is None:
            self._connect_uia()
        self._refresh_target()
        payload_limit = max(100, self.max_reply_chars - len(self.response_prefix))
        chunks = split_text(text, payload_limit)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            part = f"({index}/{total}) " if total > 1 else ""
            self._send_background(self.response_prefix + part + chunk)
        from wxauto4.uia import uiautomation as uia

        session = self._target_session(uia, force_lookup=True)
        self._target_session_signature = self._session_signature(session)
        self._target_session_unread_count = self._session_unread_count(session)

    def _send_background(self, payload: str) -> None:
        """Prefer a focus-free UIA send, with a focus-restoring fallback."""
        from wxauto4.uia import uiautomation as uia
        import win32gui

        edit = self._find_control(uia, automation_id="chat_input_field")
        if edit is None:
            raise LookupError("微信聊天输入控件不可用；请保持微信主窗口打开且不要最小化")

        value = edit.GetValuePattern()
        if value is None:
            raise LookupError("当前微信版本不支持后台输入")
        if value.Value.strip():
            raise RuntimeError("目标聊天输入框存在未发送草稿，为避免覆盖已暂停发送")

        previous_foreground = (
            win32gui.GetForegroundWindow() if self.background_mode else 0
        )
        try:
            value.SetValue(payload)
            if self._try_invoke_send(uia, value):
                return

            if value.Value != payload:
                raise RuntimeError("微信输入框内容在发送期间发生变化，已暂停发送")

            # WeChat 4.1.12 advertises InvokePattern on the send button but its
            # provider returns success without sending when the window is not
            # foreground. Fall back to briefly focusing the edit and sending
            # Enter directly to WeChat's HWND. The original foreground window
            # is restored in ``finally`` below.
            import win32api
            import win32con

            edit.SetFocus()
            hwnd = int(self._root.NativeWindowHandle)
            win32api.SendMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
            win32api.SendMessage(hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
            if self._wait_for_draft_clear(value, 3.0):
                return
            raise RuntimeError("微信后台发送未完成")
        except Exception as exc:
            # Avoid leaving an automation-generated draft after a failed send,
            # but never erase text that changed after we wrote the payload.
            if value.Value == payload:
                value.SetValue("")
                if isinstance(exc, RuntimeError) and str(exc) == "微信后台发送未完成":
                    raise RuntimeError("微信后台发送未完成，已清理输入框") from exc
            raise
        finally:
            self._restore_foreground_after_send(win32gui, previous_foreground)

    @staticmethod
    def _wait_for_draft_clear(value: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not value.Value.strip():
                return True
            time.sleep(0.1)
        return not value.Value.strip()

    def _try_invoke_send(self, uia: Any, value: Any) -> bool:
        if self._invoke_send_supported is False:
            return False

        button = self._find_control(
            uia,
            name="发送",
            control_type="ButtonControl",
        )
        if button is None:
            return False
        invoke = button.GetInvokePattern()
        if invoke is None:
            return False

        try:
            invoke.Invoke(waitTime=0)
        except Exception as exc:
            log.debug("微信发送按钮 InvokePattern 调用失败：%s", exc)

        sent = self._wait_for_draft_clear(value, 1.0)
        self._invoke_send_supported = sent
        if not sent:
            log.info("微信后台 InvokePattern 未完成发送，本次及后续发送改用聚焦回退")
        return sent

    def _restore_foreground_after_send(
        self,
        win32gui: Any,
        previous_foreground: int,
    ) -> None:
        if not self.background_mode or not previous_foreground or self._root is None:
            return

        wechat_hwnd = int(self._root.NativeWindowHandle)
        if previous_foreground == wechat_hwnd:
            return

        try:
            if win32gui.GetForegroundWindow() == wechat_hwnd:
                win32gui.SetForegroundWindow(previous_foreground)
        except Exception as exc:
            log.warning("恢复原前台窗口失败：%s", exc)

        try:
            self._send_window_to_background(win32gui)
        except Exception as exc:
            log.warning("将微信放回后台失败：%s", exc)


class _NativeParent:
    """Minimal parent contract expected by wxauto4's ChatBox parser.

    wxauto4 41.1.2 cannot initialize its top-level WeChat object against
    WeChat 4.1.12, but its message parser remains compatible. This adapter
    deliberately bypasses only the incompatible window bootstrap.
    """

    parent = None
    chat_type = "friend"

    def __init__(self, control: Any, contact: str, chat_type: str = "friend") -> None:
        self.control = control
        self.root = self
        self.nickname = contact
        self._contact = contact
        self.chat_type = chat_type

    def _lang(self, text: str) -> str:
        return text

    def chat_info(self) -> dict[str, str]:
        return {"chat_type": self.chat_type, "chat_name": self._contact}
