from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .ilink_api import (
    ILinkAPI,
    ILinkAuthenticationError,
    ILinkError,
    ILinkProtocolError,
    MAX_OUTBOUND_FILE_BYTES,
    MAX_OUTBOUND_IMAGE_BYTES,
)
from .ilink_auth import load_credentials
from .state_io import atomic_write_text


log = logging.getLogger(__name__)

MAX_PENDING_MESSAGES = 1000
MAX_SAVED_CONTEXTS = 2000
MAX_INBOUND_DEAD_LETTERS = 200
MAX_OUTBOUND_TEXT_CHUNKS = 20


class ILinkStateError(ILinkProtocolError):
    """Persistent local iLink state is unsafe to use without operator action."""


@dataclass(frozen=True)
class ReplyTarget:
    user_id: str
    context_token: str = ""


@dataclass(frozen=True)
class IncomingMessage:
    key: str
    content: str
    sender: str
    attr: str = "friend"
    conversation: str = ""
    chat_type: str = "friend"
    sender_id: str = ""
    reply_to: str = ""
    context_token: str = ""
    account_id: str = ""
    image_item: dict[str, Any] | None = None
    image_path: str = ""

    @property
    def reply_target(self) -> ReplyTarget:
        return ReplyTarget(self.reply_to or self.sender_id, self.context_token)


def _image_suffix(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ILinkProtocolError("微信图片格式不是受支持的 JPEG、PNG、GIF 或 WebP")


def split_text(text: str, limit: int) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= limit:
        return [cleaned]
    result: list[str] = []
    position = 0
    total = len(cleaned)
    while position < total:
        end = min(position + limit, total)
        cut = end
        if end < total:
            newline = cleaned.rfind("\n", position, end + 1)
            if newline >= position + limit // 2:
                cut = newline
        chunk = cleaned[position:cut].rstrip()
        if chunk:
            result.append(chunk)
        position = cut
        while position < total and cleaned[position].isspace():
            position += 1
    return result


class _StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self.empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ILinkStateError(
                f"iLink 状态文件无法读取，已停止以避免重复处理消息 {self.path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ILinkStateError(f"iLink 状态文件顶层不是 JSON 对象：{self.path}")
        raw_seen = raw.get("seen", [])
        raw_pending = raw.get("pending", [])
        raw_contexts = raw.get("contexts", {})
        raw_dead_letters = raw.get("inbound_dead_letters", [])
        if not isinstance(raw_seen, list):
            raise ILinkStateError(f"iLink 状态 seen 不是数组：{self.path}")
        if not isinstance(raw_pending, list) or any(
            not isinstance(value, dict) for value in raw_pending
        ):
            raise ILinkStateError(f"iLink 状态 pending 不是消息数组：{self.path}")
        for index, value in enumerate(raw_pending):
            if any(
                not isinstance(value.get(name), str) or not value.get(name)
                for name in ("key", "content", "sender")
            ):
                raise ILinkStateError(
                    f"iLink 状态 pending 第 {index + 1} 项缺少有效 key/content/sender："
                    f"{self.path}"
                )
        if not isinstance(raw_contexts, dict):
            raise ILinkStateError(f"iLink 状态 contexts 不是对象：{self.path}")
        if not isinstance(raw_dead_letters, list) or any(
            not isinstance(value, dict) for value in raw_dead_letters
        ):
            raise ILinkStateError(
                f"iLink 状态 inbound_dead_letters 不是对象数组：{self.path}"
            )
        state = self.empty()
        state["account_id"] = str(raw.get("account_id") or "")
        state["cursor"] = str(raw.get("cursor") or "")
        state["seen"] = [str(value) for value in raw_seen][-2000:]
        state["pending"] = [dict(value) for value in raw_pending]
        state["contexts"] = {
            str(key): str(value)
            for key, value in list(raw_contexts.items())[-MAX_SAVED_CONTEXTS:]
            if str(key)
        }
        state["last_user_id"] = str(raw.get("last_user_id") or "")
        state["inbound_dead_letters"] = [
            dict(value) for value in raw_dead_letters[-MAX_INBOUND_DEAD_LETTERS:]
        ]
        return state

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "account_id": "",
            "cursor": "",
            "seen": [],
            "pending": [],
            "contexts": {},
            "last_user_id": "",
            "inbound_dead_letters": [],
        }

    def save(self, state: dict[str, Any]) -> None:
        atomic_write_text(
            self.path,
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        )


class ILinkClient:
    def __init__(
        self,
        credentials_path: Path,
        state_path: Path,
        *,
        response_prefix: str,
        max_reply_chars: int,
        long_poll_timeout_seconds: float = 40.0,
        api_factory: type[ILinkAPI] = ILinkAPI,
    ) -> None:
        self.credentials_path = credentials_path
        self.response_prefix = response_prefix
        self.max_reply_chars = max_reply_chars
        self.long_poll_timeout_seconds = long_poll_timeout_seconds
        self._api_factory = api_factory
        self._store = _StateStore(state_path)
        self._state = self._store.load()
        self._lock = threading.RLock()
        self._incoming: queue.Queue[IncomingMessage] = queue.Queue()
        self._queued_keys: set[str] = set()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._api: ILinkAPI | None = None
        self._credentials = None
        self._worker_error: BaseException | None = None
        self._inbound_image_dir = state_path.parent / "inbound-images"

    @property
    def account_id(self) -> str:
        return self._credentials.account_id if self._credentials else ""

    @property
    def user_id(self) -> str:
        return self._credentials.user_id if self._credentials else ""

    @property
    def connected(self) -> bool:
        return (
            self._api is not None
            and self._worker is not None
            and self._worker.is_alive()
            and self._worker_error is None
        )

    @property
    def inbound_dead_letter_count(self) -> int:
        with self._lock:
            return len(self._state["inbound_dead_letters"])

    def connect(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            if self._api is not None:
                return
            raise ILinkError("原微信 iLink 长轮询仍在停止，请稍后重试")
        self.prepare_account()
        credentials = self._credentials
        assert credentials is not None

        self._api = self._api_factory(credentials.base_url, credentials.token)
        self._stop_event.clear()
        self._worker_error = None
        with self._lock:
            for raw in self._state["pending"]:
                message = self._message_from_dict(raw)
                if message is not None and message.key not in self._queued_keys:
                    self._queued_keys.add(message.key)
                    self._incoming.put(message)
        try:
            self._api.notify_start()
        except ILinkError as exc:
            log.warning("iLink 启动通知失败，继续连接：%s", exc)
        self._worker = threading.Thread(
            target=self._poll_loop,
            name="ilink-long-poll",
            daemon=True,
        )
        self._worker.start()

    def prepare_account(self) -> str:
        credentials = load_credentials(self.credentials_path)
        if credentials is None:
            raise ILinkAuthenticationError(
                "尚未登录微信 iLink，请先运行 ilink-login"
            )
        self._credentials = credentials
        self._bind_account_state(credentials.account_id)
        return credentials.account_id

    def close(self, timeout_seconds: float = 2) -> bool:
        self._stop_event.set()
        api = self._api
        if api is not None:
            try:
                api.notify_stop()
            except Exception:
                log.debug("iLink 停止通知失败", exc_info=True)
            api.close()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout_seconds)
        stopped = worker is None or not worker.is_alive()
        if stopped:
            self._worker = None
        else:
            log.warning("等待 iLink 长轮询线程停止超时")
        self._api = None
        return stopped

    def poll(self) -> list[IncomingMessage]:
        candidates: list[IncomingMessage] = []
        while True:
            try:
                candidates.append(self._incoming.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            pending_keys = {
                str(value.get("key")) for value in self._state["pending"]
            }
            for message in candidates:
                self._queued_keys.discard(message.key)
        result = [message for message in candidates if message.key in pending_keys]
        if not result and self._worker_error is not None:
            error = self._worker_error
            self._worker_error = None
            if isinstance(error, ILinkError):
                raise error
            raise ILinkError(f"iLink 长轮询异常：{type(error).__name__}: {error}") from error
        return result

    def acknowledge(self, key: str) -> None:
        with self._lock:
            state = self._state_snapshot()
            state["pending"] = [
                value for value in state["pending"] if str(value.get("key")) != key
            ]
            seen = deque((str(value) for value in state["seen"]), maxlen=2000)
            if key not in seen:
                seen.append(key)
            state["seen"] = list(seen)
            self._store.save(state)
            self._state = state
            self._queued_keys.discard(key)

    def retry(self, message: IncomingMessage) -> None:
        """Put a durable pending message back on the in-memory work queue."""
        with self._lock:
            if (
                message.key not in self._queued_keys
                and any(
                    str(value.get("key")) == message.key
                    for value in self._state["pending"]
                )
            ):
                self._queued_keys.add(message.key)
                self._incoming.put(message)

    def _bind_account_state(self, account_id: str) -> None:
        with self._lock:
            stored = str(self._state.get("account_id") or "")
            if stored and stored != account_id:
                raise ILinkStateError(
                    "iLink 状态属于其他 Bot 账号，已停止以避免跨账号污染："
                    f"state={stored!r}, credentials={account_id!r}。"
                    f"请先备份并移走 {self._store.path}"
                )
            if stored:
                return
            state = self._state_snapshot()
            state["account_id"] = account_id
            self._store.save(state)
            self._state = state

    def target_for(self, user_id: str) -> ReplyTarget:
        cleaned = user_id.strip()
        if not cleaned:
            raise ValueError("iLink 用户 ID 不能为空")
        with self._lock:
            token = str(self._state["contexts"].get(cleaned) or "")
        return ReplyTarget(cleaned, token)

    def default_target(self) -> ReplyTarget:
        with self._lock:
            user_id = str(self._state.get("last_user_id") or "")
        # Prefer the QR-authorized owner. An untrusted inbound message must not
        # be able to redirect GUI/manual sends merely by becoming "last seen".
        user_id = self.user_id or user_id
        if not user_id:
            raise ILinkProtocolError("还没有可发送的 iLink 用户")
        return self.target_for(user_id)

    @staticmethod
    def _client_id(value: str | None = None, *, part: str = "") -> str:
        base = (value or "").strip() or f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        if not base.startswith("wechat-codex:"):
            base = f"wechat-codex:{base}"
        return f"{base}:{part}" if part else base

    def send(
        self,
        text: str,
        target: ReplyTarget | None = None,
        *,
        client_id: str | None = None,
    ) -> None:
        api = self._api
        if api is None:
            self.connect()
            api = self._api
        assert api is not None
        resolved = target or self.default_target()
        payload_limit = max(100, self.max_reply_chars - len(self.response_prefix) - 16)
        cleaned = text.strip()
        hard_limit = payload_limit * MAX_OUTBOUND_TEXT_CHUNKS
        truncated = len(cleaned) > hard_limit
        chunks = split_text(cleaned[:hard_limit], payload_limit)
        if len(chunks) > MAX_OUTBOUND_TEXT_CHUNKS:
            chunks = chunks[:MAX_OUTBOUND_TEXT_CHUNKS]
            truncated = True
        if truncated and chunks:
            note = "\n[回复过长，已截断]"
            final = chunks[-1]
            if len(final) + len(note) > payload_limit:
                final = final[: payload_limit - len(note)].rstrip()
            chunks[-1] = final + note
        for index, chunk in enumerate(chunks, start=1):
            part = f"({index}/{len(chunks)}) " if len(chunks) > 1 else ""
            part_id = f"{index}-{len(chunks)}" if len(chunks) > 1 else ""
            api.send_message(
                {
                    "from_user_id": "",
                    "to_user_id": resolved.user_id,
                    "client_id": self._client_id(client_id, part=part_id),
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": resolved.context_token or None,
                    "item_list": [
                        {
                            "type": 1,
                            "text_item": {"text": self.response_prefix + part + chunk},
                        }
                    ],
                }
            )

    def send_image(
        self,
        image_path: str | Path,
        target: ReplyTarget | None = None,
        *,
        client_id: str | None = None,
    ) -> None:
        api = self._api
        if api is None:
            self.connect()
            api = self._api
        assert api is not None
        resolved = target or self.default_target()
        path = Path(image_path).expanduser().resolve()
        if not path.is_file():
            raise ILinkProtocolError(f"待发送的图片不存在：{path}")
        if path.stat().st_size > MAX_OUTBOUND_IMAGE_BYTES:
            raise ILinkProtocolError("待发送的图片超过 20 MB 限制")
        image_item = api.upload_image(path.read_bytes(), resolved.user_id)
        api.send_message(
            {
                "from_user_id": "",
                "to_user_id": resolved.user_id,
                "client_id": self._client_id(client_id),
                "message_type": 2,
                "message_state": 2,
                "context_token": resolved.context_token or None,
                "item_list": [{"type": 2, "image_item": image_item}],
            }
        )

    def send_file(
        self,
        file_path: str | Path,
        target: ReplyTarget | None = None,
        *,
        client_id: str | None = None,
    ) -> None:
        api = self._api
        if api is None:
            self.connect()
            api = self._api
        assert api is not None
        resolved = target or self.default_target()
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise ILinkProtocolError(f"待发送的文件不存在：{path}")
        if path.stat().st_size > MAX_OUTBOUND_FILE_BYTES:
            raise ILinkProtocolError("待发送的文件超过 20 MB 限制")
        file_item = api.upload_file(path.read_bytes(), resolved.user_id, path.name)
        api.send_message(
            {
                "from_user_id": "",
                "to_user_id": resolved.user_id,
                "client_id": self._client_id(client_id),
                "message_type": 2,
                "message_state": 2,
                "context_token": resolved.context_token or None,
                "item_list": [{"type": 4, "file_item": file_item}],
            }
        )

    def reconnect(self) -> None:
        """Close the current API/poller and establish a fresh connection."""
        if not self.close(timeout_seconds=10):
            raise ILinkError("等待原微信 iLink 长轮询停止超时，未启动重复连接")
        self.connect()

    def materialize_image(self, message: IncomingMessage) -> IncomingMessage:
        if message.image_path or message.image_item is None:
            return message
        api = self._api
        if api is None:
            raise ILinkProtocolError("iLink 尚未连接，无法下载微信图片")

        data = api.download_image(message.image_item)
        suffix = _image_suffix(data)
        digest = hashlib.sha256(message.key.encode("utf-8")).hexdigest()[:24]
        self._inbound_image_dir.mkdir(parents=True, exist_ok=True)
        destination = self._inbound_image_dir / f"{digest}{suffix}"
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        updated = replace(message, image_path=str(destination.resolve()))

        with self._lock:
            state = self._state_snapshot()
            state["pending"] = [
                asdict(updated) if str(value.get("key")) == message.key else value
                for value in state["pending"]
            ]
            self._store.save(state)
            self._state = state
        return updated

    def _poll_loop(self) -> None:
        assert self._api is not None
        timeout = self.long_poll_timeout_seconds
        failures = 0
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    pending_count = len(self._state["pending"])
                    cursor = str(self._state.get("cursor") or "")
                if pending_count >= MAX_PENDING_MESSAGES:
                    log.warning(
                        "iLink 待处理消息已达到上限 %d，暂停拉取以施加背压",
                        MAX_PENDING_MESSAGES,
                    )
                    self._stop_event.wait(1)
                    continue
                response = self._api.get_updates(cursor, timeout)
                suggested = response.get("longpolling_timeout_ms")
                if isinstance(suggested, (int, float)) and suggested > 0:
                    timeout = min(60.0, max(10.0, float(suggested) / 1000.0))
                self._record_response(response)
                failures = 0
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                failures += 1
                log.warning("iLink 长轮询失败（%d/3）：%s", failures, exc)
                if isinstance(exc, ILinkAuthenticationError):
                    self._worker_error = exc
                    return
                if failures >= 3:
                    self._worker_error = exc
                    return
                self._stop_event.wait(2)

    def _record_response(self, response: dict[str, Any]) -> None:
        messages: list[IncomingMessage] = []
        with self._lock:
            state = self._state_snapshot()
            seen = {str(value) for value in state["seen"]}
            pending = {str(value.get("key")) for value in state["pending"]}
            raw_messages = response.get("msgs")
            if raw_messages is None:
                raw_messages = []
            if not isinstance(raw_messages, list):
                raise ILinkProtocolError("iLink 长轮询 msgs 不是 JSON 数组")
            for raw in raw_messages:
                if not isinstance(raw, dict):
                    raise ILinkProtocolError("iLink 长轮询消息不是 JSON 对象")
                message = self._normalize(raw)
                if message is None:
                    if raw.get("message_type") == 1:
                        self._record_inbound_dead_letter(state, raw)
                    continue
                if message.key in seen or message.key in pending:
                    continue
                if message.context_token:
                    state["contexts"].pop(message.reply_to, None)
                    state["contexts"][message.reply_to] = message.context_token
                    while len(state["contexts"]) > MAX_SAVED_CONTEXTS:
                        oldest = next(iter(state["contexts"]))
                        if oldest == self.user_id and len(state["contexts"]) > 1:
                            state["contexts"][oldest] = state["contexts"].pop(oldest)
                            continue
                        state["contexts"].pop(oldest, None)
                state["last_user_id"] = message.reply_to
                state["pending"].append(asdict(message))
                pending.add(message.key)
                messages.append(message)
            next_cursor = response.get("get_updates_buf")
            if isinstance(next_cursor, str) and next_cursor:
                state["cursor"] = next_cursor
            self._store.save(state)
            self._state = state
            for message in messages:
                if message.key not in self._queued_keys:
                    self._queued_keys.add(message.key)
                    self._incoming.put(message)

    @staticmethod
    def _record_inbound_dead_letter(
        state: dict[str, Any], raw: dict[str, Any]
    ) -> None:
        item_types = [
            value.get("type")
            for value in (raw.get("item_list") or [])
            if isinstance(value, dict)
        ]
        state["inbound_dead_letters"].append(
            {
                "received_at": time.time(),
                "message_id": str(
                    raw.get("message_id") or raw.get("seq") or raw.get("client_id") or ""
                )[:200],
                "from_user_id": str(raw.get("from_user_id") or "")[:200],
                "message_type": raw.get("message_type"),
                "item_types": item_types[:20],
                "reason": "无法解析受支持的文本、语音或图片内容",
            }
        )
        state["inbound_dead_letters"] = state["inbound_dead_letters"][
            -MAX_INBOUND_DEAD_LETTERS:
        ]
        log.warning(
            "iLink 入站消息无法解析，已记录死信：message_id=%s, item_types=%s",
            state["inbound_dead_letters"][-1]["message_id"],
            item_types,
        )

    def _state_snapshot(self) -> dict[str, Any]:
        """Return a detached state copy for save-before-commit updates."""
        return {
            "account_id": str(self._state.get("account_id") or ""),
            "cursor": str(self._state.get("cursor") or ""),
            "seen": list(self._state["seen"]),
            "pending": [dict(value) for value in self._state["pending"]],
            "contexts": dict(self._state["contexts"]),
            "last_user_id": str(self._state.get("last_user_id") or ""),
            "inbound_dead_letters": [
                dict(value) for value in self._state["inbound_dead_letters"]
            ],
        }

    def _normalize(self, raw: dict[str, Any]) -> IncomingMessage | None:
        if raw.get("message_type") != 1:
            return None
        sender_id = str(raw.get("from_user_id") or "").strip()
        if not sender_id:
            return None
        identity = raw.get("message_id") or raw.get("seq") or raw.get("client_id")
        if identity in (None, ""):
            encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
            identity = hashlib.sha256(encoded).hexdigest()
        parts: list[str] = []
        image_item: dict[str, Any] | None = None
        for item in raw.get("item_list") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == 1:
                text = (item.get("text_item") or {}).get("text")
            elif item.get("type") == 2:
                candidate = item.get("image_item")
                if image_item is None and isinstance(candidate, dict):
                    image_item = candidate
                    parts.append("[图片]")
                text = None
            elif item.get("type") == 3:
                text = (item.get("voice_item") or {}).get("text")
            else:
                text = None
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        content = "\n".join(parts).strip()
        if not content:
            return None
        return IncomingMessage(
            key=f"ilink:{identity}",
            content=content,
            sender=sender_id,
            conversation=sender_id,
            sender_id=sender_id,
            reply_to=sender_id,
            context_token=str(raw.get("context_token") or ""),
            account_id=self.account_id,
            image_item=image_item,
        )

    @staticmethod
    def _message_from_dict(raw: dict[str, Any]) -> IncomingMessage | None:
        required = ("key", "content", "sender")
        if any(not isinstance(raw.get(name), str) for name in required):
            return None
        allowed = IncomingMessage.__dataclass_fields__
        try:
            values = {
                name: value for name, value in raw.items() if name in allowed
            }
            image_item = values.get("image_item")
            if image_item is not None and not isinstance(image_item, dict):
                values["image_item"] = None
            return IncomingMessage(**values)
        except TypeError:
            return None
