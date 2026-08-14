from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WxCliUnavailable(RuntimeError):
    """The configured wx-cli executable cannot be found."""


class WxCliReadError(RuntimeError):
    """wx-cli could not return a valid incremental message response."""


@dataclass(frozen=True)
class WxCliMessage:
    key: str
    content: str
    sender: str
    conversation: str
    chat_type: str
    is_self: bool


class WxCliReader:
    """Read incremental messages from an externally installed wx-compatible CLI.

    This project deliberately does not install, initialize, or manage the external
    program. It only consumes the JSON contract of ``new-messages``.
    """

    _CANDIDATES = ("wx", "wx-cli", "wechat-cli")

    def __init__(
        self,
        *,
        contact: str,
        chat_type: str,
        executable: str | None = None,
        username: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.contact = contact.strip()
        self.chat_type = chat_type
        self.configured_executable = (executable or "").strip() or None
        self.username = (username or "").strip() or None
        self.timeout_seconds = timeout_seconds
        self._executable: str | None = None
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()
        self._seen_limit = 2000

    @property
    def executable(self) -> str | None:
        return self._executable

    def connect(self) -> None:
        self._executable = self._resolve_executable()

    def baseline(self) -> int:
        """Advance wx-cli's cursor once and discard the returned history."""
        messages = self._read_target_messages()
        for message in messages:
            self._remember(message.key)
        return len(messages)

    def poll(self) -> list[WxCliMessage]:
        incoming: list[WxCliMessage] = []
        for message in self._read_target_messages():
            if message.key in self._seen:
                continue
            self._remember(message.key)
            incoming.append(message)
        return incoming

    def _remember(self, key: str) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > self._seen_limit:
            self._seen.discard(self._seen_order.popleft())

    def _resolve_executable(self) -> str:
        if self.configured_executable:
            configured = Path(self.configured_executable).expanduser()
            if configured.is_file():
                return str(configured.resolve())
            resolved = shutil.which(self.configured_executable)
            if resolved:
                return resolved
            raise WxCliUnavailable(
                "找不到配置的 wx-cli 可执行文件："
                f"{self.configured_executable}。请检查 wx_cli_path。"
            )

        for candidate in self._CANDIDATES:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        raise WxCliUnavailable(
            "找不到 wx、wx-cli 或 wechat-cli 可执行文件。"
            "请先自行安装并初始化兼容工具，再在 config.yaml 设置 wx_cli_path。"
        )

    def _read_target_messages(self) -> list[WxCliMessage]:
        payload = self._run_new_messages()
        raw_messages = self._message_list(payload)
        occurrences: Counter[str] = Counter()
        messages: list[WxCliMessage] = []
        for raw in raw_messages:
            if not isinstance(raw, dict) or not self._matches_target(raw):
                continue
            message = self._normalize(raw, occurrences)
            if message is not None:
                messages.append(message)
        return messages

    def _run_new_messages(self) -> Any:
        executable = self._executable
        if executable is None:
            self.connect()
            executable = self._executable
        assert executable is not None

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [executable, "new-messages", "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
                creationflags=creation_flags,
            )
        except FileNotFoundError as exc:
            self._executable = None
            raise WxCliUnavailable(f"wx-cli 可执行文件已不存在：{executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise WxCliReadError(
                f"wx-cli 读取消息超过 {self.timeout_seconds:g} 秒"
            ) from exc
        except OSError as exc:
            raise WxCliReadError(f"无法启动 wx-cli：{exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            if len(detail) > 500:
                detail = detail[:500] + "…"
            raise WxCliReadError(
                f"wx-cli new-messages 失败（退出码 {completed.returncode}）"
                + (f"：{detail}" if detail else "")
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise WxCliReadError("wx-cli new-messages 没有返回有效 JSON") from exc

    @staticmethod
    def _message_list(payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise WxCliReadError("wx-cli JSON 顶层必须是对象或数组")

        messages = payload.get("messages")
        if isinstance(messages, list):
            return messages
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return data["messages"]
        raise WxCliReadError("wx-cli JSON 中缺少 messages 数组")

    def _matches_target(self, raw: dict[str, Any]) -> bool:
        if self.username:
            if str(raw.get("username", "")).strip() != self.username:
                return False
        elif str(raw.get("chat", "")).strip() != self.contact:
            return False

        raw_type = str(raw.get("chat_type", "")).strip().lower()
        if not raw_type:
            raw_type = "group" if bool(raw.get("is_group")) else "private"
        elif raw_type == "friend":
            raw_type = "private"
        expected = {"friend": "private", "group": "group"}.get(self.chat_type)
        if expected is None:
            return raw_type in {"private", "group"}
        return raw_type == expected

    def _normalize(
        self,
        raw: dict[str, Any],
        occurrences: Counter[str],
    ) -> WxCliMessage | None:
        message_type = str(raw.get("type", "")).strip().lower()
        if message_type not in {"text", "quote"}:
            return None
        content = raw.get("content")
        if not isinstance(content, str) or not content.strip():
            return None

        conversation = str(raw.get("chat", "")).strip() or self.contact
        raw_chat_type = str(raw.get("chat_type", "")).strip().lower()
        chat_type = "group" if raw_chat_type == "group" or raw.get("is_group") else "friend"
        sender = str(raw.get("sender", "")).strip()

        explicit_self = raw.get("is_self")
        if isinstance(explicit_self, bool):
            is_self = explicit_self
        else:
            # The known wx JSON contract leaves the sender blank for incoming
            # private messages and labels messages sent by the logged-in user.
            # Group senders are member labels, so they cannot be inferred here.
            is_self = chat_type == "friend" and bool(sender) and sender != conversation

        fingerprint = "|".join(
            (
                str(raw.get("username", "")),
                str(raw.get("timestamp", "")),
                sender,
                content.strip(),
                message_type,
            )
        )
        occurrence = occurrences[fingerprint]
        occurrences[fingerprint] += 1
        digest = hashlib.sha256(fingerprint.encode("utf-8", errors="replace")).hexdigest()
        key = f"wx-cli:{digest}:{occurrence}"

        return WxCliMessage(
            key=key,
            content=content.strip(),
            sender=sender,
            conversation=conversation,
            chat_type=chat_type,
            is_self=is_self,
        )
