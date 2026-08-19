from __future__ import annotations

import threading
import time
from typing import Any

from .chatgpt_runner import ChatEvent, ChatGPTRunner


class SharedChatGPTPool:
    """One ChatGPT browser worker shared by multiple iLink accounts."""

    def __init__(self, runner: ChatGPTRunner) -> None:
        self.runner = runner
        self._lock = threading.RLock()
        self._accounts: set[str] = set()
        self._sessions: dict[str, str] = {}

    def for_account(self, account_id: str) -> SharedChatGPTAccount:
        cleaned = account_id.strip()
        if not cleaned:
            raise ValueError("共享 ChatGPT 账号 ID 不能为空")
        with self._lock:
            self._accounts.add(cleaned)
        return SharedChatGPTAccount(self, cleaned)

    def register_session(self, account_id: str, session_key: str) -> None:
        if not session_key:
            raise ValueError("共享 ChatGPT 会话键不能为空")
        with self._lock:
            existing = self._sessions.get(session_key)
            if existing is not None and existing != account_id:
                raise RuntimeError("ChatGPT 会话不能跨微信账号复用")
            self._sessions[session_key] = account_id

    def session_owner(self, session_key: str | None) -> str | None:
        if not session_key:
            return None
        with self._lock:
            owner = self._sessions.get(session_key)
            if owner is not None:
                return owner
            matches = [
                account_id
                for account_id in self._accounts
                if session_key.startswith(f"ilink:{account_id}:")
            ]
            if len(matches) == 1:
                self._sessions[session_key] = matches[0]
                return matches[0]
            return None

    def session_keys(self, account_id: str) -> frozenset[str]:
        with self._lock:
            return frozenset(
                key for key, owner in self._sessions.items() if owner == account_id
            )

    def drain_for(self, account_id: str) -> list[ChatEvent]:
        # Only remove this account's events. Foreign events are put back on the
        # runner queue, so a stopped/error account retains recovery ownership
        # instead of having its completion stranded in another proxy's memory.
        with self._lock:
            drained = self.runner.drain_events()
            result: list[ChatEvent] = []
            foreign: list[ChatEvent] = []
            for event in drained:
                if self.session_owner(event.session_key) == account_id:
                    result.append(event)
                else:
                    foreign.append(event)
            for event in foreign:
                self.runner.events.put(event)
            return result

    def close(self, timeout_seconds: float = 10) -> None:
        self.runner.close(timeout_seconds)


class SharedChatGPTAccount:
    """Account-scoped view of the application-level ChatGPTRunner."""

    shared = True
    _SESSION_METHODS = frozenset({
        "begin_chat",
        "begin_reset",
        "begin_archive",
        "begin_rename",
        "begin_retry",
        "begin_export",
        "begin_summary",
        "conversation_info",
        "conversation_list",
        "switch_conversation",
        "last_reply",
    })

    def __init__(self, pool: SharedChatGPTPool, account_id: str) -> None:
        self._pool = pool
        self.account_id = account_id

    @property
    def _runner(self) -> ChatGPTRunner:
        return self._pool.runner

    @property
    def active(self) -> bool:
        # This is deliberately global so every account queues behind the one
        # persistent Plus browser instead of trying to open a second profile.
        return self._runner.active

    @property
    def browser_running(self) -> bool:
        return self._runner.browser_running

    @property
    def owns_active(self) -> bool:
        return (
            self._pool.session_owner(self._runner.active_session_key)
            == self.account_id
        )

    def start(self) -> None:
        self._runner.start()

    def drain_events(self) -> list[ChatEvent]:
        return self._pool.drain_for(self.account_id)

    def stop(self) -> str:
        return self._runner.stop_if_session_owned(
            self._pool.session_keys(self.account_id)
        )

    def close(self, timeout_seconds: float = 10) -> None:
        # BridgeApp calls close before its final completion drain.  Wait for an
        # account-owned cancellation to publish its last event, but never close
        # the application-level worker or wait on another account's request.
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            active_key = self._runner.active_session_key
            if self._pool.session_owner(active_key) != self.account_id:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._runner, name)
        if name not in self._SESSION_METHODS or not callable(attribute):
            return attribute

        def account_scoped(*args: Any, **kwargs: Any) -> Any:
            session_key = args[0] if args else kwargs.get("session_key")
            if not isinstance(session_key, str) or not session_key:
                raise ValueError(f"{name} 缺少有效的 ChatGPT 会话键")
            self._pool.register_session(self.account_id, session_key)
            return attribute(*args, **kwargs)

        return account_scoped
