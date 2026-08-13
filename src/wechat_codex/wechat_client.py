from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class IncomingMessage:
    key: str
    content: str
    sender: str
    attr: str


def _message_key(message: Any) -> str:
    for name in ("hash", "id", "hash_text"):
        value = getattr(message, name, None)
        if value not in (None, ""):
            return f"{name}:{value}"
    raw = "|".join(
        str(getattr(message, name, "")) for name in ("attr", "sender", "type", "content")
    )
    return "fallback:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def normalize_message(message: Any) -> IncomingMessage | None:
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        return None

    attr = str(getattr(message, "attr", "")).lower()
    class_name = type(message).__name__.lower()
    if not attr:
        if "friend" in class_name:
            attr = "friend"
        elif "self" in class_name:
            attr = "self"

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
        allow_self_messages: bool,
        response_prefix: str,
        max_reply_chars: int,
    ) -> None:
        self.contact = contact
        self.allow_self_messages = allow_self_messages
        self.response_prefix = response_prefix
        self.max_reply_chars = max_reply_chars
        self._wx: Any = None
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()
        self._seen_limit = 1000
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
                if pid not in pids or not win32gui.IsWindowVisible(hwnd):
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

            self._root.SetActive()
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
            session.Click()
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
            result.Click()
        else:
            search.Click()
            uia.SendKeys("{ENTER}")
        time.sleep(0.8)
        value.SetValue("")
        if self._current_contact(uia) != self.contact:
            raise LookupError(f"未能切换到联系人 {self.contact!r}")

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
        return self._chatbox.get_msgs()

    def baseline(self) -> int:
        count = 0
        for raw in self._all_messages():
            message = normalize_message(raw)
            if message:
                self._remember(message.key)
                count += 1
        return count

    def poll(self) -> list[IncomingMessage]:
        incoming: list[IncomingMessage] = []
        for raw in self._all_messages():
            message = normalize_message(raw)
            if not message or message.key in self._seen:
                continue
            self._remember(message.key)
            if message.content.startswith(self.response_prefix):
                continue
            if message.attr == "self" and not self.allow_self_messages:
                continue
            incoming.append(message)
        return incoming

    def send(self, text: str) -> None:
        if self._wx is None:
            raise RuntimeError("微信客户端尚未连接")
        self._refresh_target()
        payload_limit = max(100, self.max_reply_chars - len(self.response_prefix))
        chunks = split_text(text, payload_limit)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            part = f"({index}/{total}) " if total > 1 else ""
            response = self._chatbox.send_msg(self.response_prefix + part + chunk)
            if response is not None and hasattr(response, "is_success") and not response.is_success:
                raise RuntimeError(f"微信发送失败：{response}")


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
