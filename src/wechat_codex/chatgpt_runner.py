from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)


CHAT_INSTRUCTIONS = (
    "你是微信里的中文聊天助手。回答适合微信阅读，简洁、直接、准确。"
    "你不能读取或修改用户本地文件，不能执行本地命令；需要操作项目时，"
    "请提示用户使用“干活：任务”命令交给 Codex。"
)


@dataclass(frozen=True)
class ChatEvent:
    text: str


@dataclass
class ChatState:
    active: bool = False
    operation: str = "idle"
    session_key: str | None = None
    started_at: float | None = None
    client: Any = None
    stop_requested: bool = False


class ChatGPTRunner:
    def __init__(
        self,
        *,
        model: str,
        reasoning_effort: str,
        max_output_tokens: int,
        timeout_seconds: int,
        runtime_dir: Path,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.runtime_dir = runtime_dir
        self.events: queue.Queue[ChatEvent] = queue.Queue()
        self._client_factory = client_factory
        self._state = ChatState()
        self._lock = threading.RLock()
        self._conversation_file = runtime_dir / "chat_conversations.json"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._conversations = self._load_conversations()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state.active

    def begin_chat(self, session_key: str, prompt: str) -> tuple[bool, str]:
        return self._begin("chat", session_key, self._run_chat, prompt)

    def begin_reset(self, session_key: str) -> tuple[bool, str]:
        with self._lock:
            conversation_id = self._conversations.get(session_key)
        if not conversation_id:
            return False, "当前已经是新的 ChatGPT 对话"
        return self._begin("reset", session_key, self._run_reset, conversation_id)

    def _begin(
        self,
        operation: str,
        session_key: str,
        target: Callable[[str, str], None],
        value: str,
    ) -> tuple[bool, str]:
        with self._lock:
            if self._state.active:
                return False, "已有 ChatGPT 请求在执行，请发送“状态”或“停止”"
            self._state = ChatState(
                active=True,
                operation=operation,
                session_key=session_key,
                started_at=time.time(),
            )

        worker = threading.Thread(
            target=target,
            args=(session_key, value),
            name=f"chatgpt-{operation}",
            daemon=True,
        )
        worker.start()
        return True, "已开始 ChatGPT 请求"

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("未设置环境变量 OPENAI_API_KEY，无法使用 ChatGPT 聊天")

        from openai import OpenAI

        return OpenAI(timeout=self.timeout_seconds, max_retries=1)

    def _attach_client(self, client: Any) -> bool:
        with self._lock:
            self._state.client = client
            stopped = self._state.stop_requested
        if stopped:
            self._close_client(client)
        return not stopped

    def _run_chat(self, session_key: str, prompt: str) -> None:
        client: Any = None
        try:
            client = self._new_client()
            if not self._attach_client(client):
                self.events.put(ChatEvent("ChatGPT 请求已停止"))
                return

            conversation_id = self._get_or_create_conversation(client, session_key)
            try:
                response = self._create_response(client, conversation_id, prompt)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 404 or self._stopped():
                    raise
                log.warning("ChatGPT 持久对话已失效，正在创建新对话：%s", exc)
                self._remove_conversation(session_key)
                conversation_id = self._get_or_create_conversation(client, session_key)
                response = self._create_response(client, conversation_id, prompt)

            if self._stopped():
                self.events.put(ChatEvent("ChatGPT 请求已停止"))
                return

            text = str(getattr(response, "output_text", "") or "").strip()
            if not text:
                raise RuntimeError("ChatGPT 没有返回文本结果")
            self._log_usage(response)
            self.events.put(ChatEvent(text))
        except Exception as exc:
            if self._stopped():
                self.events.put(ChatEvent("ChatGPT 请求已停止"))
            else:
                detail = str(exc).strip() or type(exc).__name__
                self.events.put(ChatEvent(f"ChatGPT 请求失败：{detail[-1000:]}"))
        finally:
            self._close_client(client)
            self._finish()

    def _run_reset(self, session_key: str, conversation_id: str) -> None:
        client: Any = None
        try:
            client = self._new_client()
            if not self._attach_client(client):
                self.events.put(ChatEvent("ChatGPT 请求已停止"))
                return
            try:
                client.conversations.delete(conversation_id)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 404:
                    raise
                log.info("ChatGPT 远端对话已不存在，清理本地映射：%s", conversation_id)
            self._remove_conversation(session_key, expected_id=conversation_id)
            self.events.put(ChatEvent("已新建 ChatGPT 对话，后续消息将使用新的上下文"))
        except Exception as exc:
            if self._stopped():
                self.events.put(ChatEvent("ChatGPT 请求已停止"))
            else:
                detail = str(exc).strip() or type(exc).__name__
                self.events.put(ChatEvent(f"重置 ChatGPT 对话失败：{detail[-1000:]}"))
        finally:
            self._close_client(client)
            self._finish()

    def _create_response(self, client: Any, conversation_id: str, prompt: str) -> Any:
        return client.responses.create(
            model=self.model,
            conversation=conversation_id,
            instructions=CHAT_INSTRUCTIONS,
            input=[{"role": "user", "content": prompt}],
            reasoning={"effort": self.reasoning_effort},
            max_output_tokens=self.max_output_tokens,
        )

    def _get_or_create_conversation(self, client: Any, session_key: str) -> str:
        with self._lock:
            existing = self._conversations.get(session_key)
        if existing:
            return existing

        conversation = client.conversations.create(
            metadata={"source": "wechat-codex-control"}
        )
        conversation_id = str(conversation.id)
        self._set_conversation(session_key, conversation_id)
        return conversation_id

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
                if str(key) and str(value)
            }
        except Exception as exc:
            log.warning("忽略无法读取的 ChatGPT 对话映射 %s：%s", self._conversation_file, exc)
            return {}

    def _set_conversation(self, session_key: str, conversation_id: str) -> None:
        with self._lock:
            self._conversations[session_key] = conversation_id
            snapshot = dict(self._conversations)
        self._save_conversations(snapshot)

    def _remove_conversation(
        self,
        session_key: str,
        *,
        expected_id: str | None = None,
    ) -> None:
        with self._lock:
            current = self._conversations.get(session_key)
            if expected_id is not None and current != expected_id:
                return
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

    def _log_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        log.info(
            "ChatGPT 用量：model=%s input=%s output=%s total=%s",
            self.model,
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            getattr(usage, "total_tokens", None),
        )

    def _stopped(self) -> bool:
        with self._lock:
            return self._state.stop_requested

    @staticmethod
    def _close_client(client: Any) -> None:
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                log.debug("关闭 OpenAI 客户端失败", exc_info=True)

    def _finish(self) -> None:
        with self._lock:
            self._state.active = False
            self._state.client = None
            self._state.stop_requested = False

    def stop(self) -> str:
        with self._lock:
            if not self._state.active:
                return "当前没有执行中的 ChatGPT 请求"
            self._state.stop_requested = True
            client = self._state.client
        self._close_client(client)
        return "正在停止 ChatGPT 请求"

    def status(self) -> str:
        with self._lock:
            state = self._state
            if not state.active:
                return "ChatGPT 当前空闲"
            elapsed = int(time.time() - (state.started_at or time.time()))
            label = "重置对话" if state.operation == "reset" else "聊天"
            return f"正在执行 ChatGPT {label}，已运行 {elapsed} 秒"

    def drain_events(self) -> list[ChatEvent]:
        result: list[ChatEvent] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result
