from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingMessage:
    key: str
    content: str
    sender: str
    attr: str


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
    ) -> None:
        self.contact = contact
        self.background_mode = background_mode
        self.allow_self_messages = allow_self_messages
        self.voice_recognition = voice_recognition
        self.voice_retry_count = voice_retry_count
        self.response_prefix = response_prefix
        self.max_reply_chars = max_reply_chars
        self._wx: Any = None
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()
        self._seen_limit = 1000
        self._voice_failures: dict[str, tuple[int, float]] = {}
        self._root: Any = None
        self._chatbox: Any = None

    def connect(self) -> None:
        try:
            import psutil
            import win32gui
            import win32process
            from wxauto4.uia import uiautomation as uia
            from wxauto4.ui.chatbox import ChatBox
        except ImportError as exc:  # pragma: no cover - depends on Windows package install
            raise RuntimeError("未安装 wxauto4，请运行 pip install -e .") from exc

        try:
            pids = {
                process.info["pid"]
                for process in psutil.process_iter(["name", "pid"])
                if (process.info.get("name") or "").lower() == "weixin.exe"
            }
            handles: list[int] = []

            def collect(hwnd: int, _: Any) -> None:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid not in pids:
                    return
                if "QWindowIcon" in win32gui.GetClassName(hwnd):
                    handles.append(hwnd)

            win32gui.EnumWindows(collect, None)
            self._root = None
            for hwnd in handles:
                candidate = uia.ControlFromHandle(hwnd)
                if candidate and candidate.ClassName == "mmui::MainWindow":
                    self._root = candidate
                    break
            if self._root is None:
                raise LookupError("未找到已登录的微信主窗口")

            if self.background_mode:
                self._send_window_to_background(win32gui)
            self._ensure_contact(uia)
            page = self._find_control(uia, automation_id="chat_message_page")
            if page is None:
                raise LookupError("未找到聊天页面")

            parent = _NativeParent(self._root, self.contact)
            self._chatbox = ChatBox(page, parent)
            self._wx = self._chatbox
        except Exception as exc:
            raise RuntimeError(
                "无法连接 PC 微信。请确认微信 4.x 已登录、窗口未退出，并检查 contact 名称。"
                f"原始错误：{type(exc).__name__}: {exc}"
            ) from exc

    def _send_window_to_background(self, win32gui: Any) -> None:
        """Keep WeChat behind other windows without activating or hiding it."""
        import win32con

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

    def _ensure_contact(self, uia: Any) -> None:
        if self._current_contact(uia) == self.contact:
            return

        session = self._find_control(
            uia,
            automation_id=f"session_item_{self.contact}",
        )
        if session is not None:
            self._select_without_focus(session)
            time.sleep(0.5)
            if self._current_contact(uia) == self.contact:
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
        time.sleep(1)
        result = self._find_control(
            uia,
            name=self.contact,
            control_type="ListItemControl",
            prefix_name=True,
        )
        if result is not None:
            self._select_without_focus(result)
        else:
            value.SetValue("")
            raise LookupError(f"微信搜索结果中没有联系人 {self.contact!r}")
        time.sleep(0.8)
        value.SetValue("")
        if self._current_contact(uia) != self.contact:
            raise LookupError(f"未能切换到联系人 {self.contact!r}")

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
        page = self._find_control(uia, automation_id="chat_message_page")
        if page is None:
            raise LookupError("未找到聊天页面")
        self._chatbox = ChatBox(page, _NativeParent(self._root, self.contact))
        self._wx = self._chatbox

    def _remember(self, key: str) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > self._seen_limit:
            self._seen.discard(self._seen_order.popleft())

    def _all_messages(self) -> Iterable[Any]:
        if self._wx is None:
            raise RuntimeError("微信客户端尚未连接")
        self._refresh_target()
        if not self.background_mode:
            return self._chatbox.get_msgs()
        return self._native_messages()

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
        count = 0
        for raw in self._all_messages():
            message = normalize_message(raw)
            if message or is_voice_message(raw):
                self._remember(_message_key(raw))
                count += 1
        return count

    def poll(self) -> list[IncomingMessage]:
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
            incoming.append(message)
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
        if self._wx is None:
            raise RuntimeError("微信客户端尚未连接")
        self._refresh_target()
        payload_limit = max(100, self.max_reply_chars - len(self.response_prefix))
        chunks = split_text(text, payload_limit)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            part = f"({index}/{total}) " if total > 1 else ""
            self._send_background(self.response_prefix + part + chunk)

    def _send_background(self, payload: str) -> None:
        """Send without clipboard, mouse movement, simulated keys, or focus changes."""
        from wxauto4.uia import uiautomation as uia

        edit = self._find_control(uia, automation_id="chat_input_field")
        if edit is None:
            raise LookupError("微信聊天输入控件不可用；请保持微信主窗口打开且不要最小化")

        value = edit.GetValuePattern()
        if value is None:
            raise LookupError("当前微信版本不支持后台输入")
        if value.Value.strip():
            raise RuntimeError("目标聊天输入框存在未发送草稿，为避免覆盖已暂停发送")

        value.SetValue(payload)
        try:
            # WeChat 4.1.12 advertises InvokePattern on the send button but its
            # provider returns success without sending when the window is not
            # foreground. Give the edit logical focus inside WeChat, then post
            # Enter directly to WeChat's HWND. This does not synthesize a
            # system-wide key event and does not activate the window.
            import win32api
            import win32con

            edit.SetFocus()
            hwnd = int(self._root.NativeWindowHandle)
            win32api.SendMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
            win32api.SendMessage(hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
        except Exception:
            # Avoid leaving an automation-generated draft after a failed send.
            if value.Value == payload:
                value.SetValue("")
            raise

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not value.Value.strip():
                return
            time.sleep(0.1)
        if value.Value == payload:
            value.SetValue("")
        raise RuntimeError("微信后台发送未完成，已清理输入框")


class _NativeParent:
    """Minimal parent contract expected by wxauto4's ChatBox parser.

    wxauto4 41.1.2 cannot initialize its top-level WeChat object against
    WeChat 4.1.12, but its message parser remains compatible. This adapter
    deliberately bypasses only the incompatible window bootstrap.
    """

    parent = None
    chat_type = "friend"

    def __init__(self, control: Any, contact: str) -> None:
        self.control = control
        self.root = self
        self.nickname = contact
        self._contact = contact

    def _lang(self, text: str) -> str:
        return text

    def chat_info(self) -> dict[str, str]:
        return {"chat_type": "friend", "chat_name": self._contact}
