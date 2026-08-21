from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import queue
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .chat_history import ChatHistoryError, ChatMessage
from .chatgpt_runner import ChatGPTRunner
from .codex_runner import CodexRunner
from .config import AppConfig
from .ilink_api import ILinkError
from .ilink_client import ILinkClient, ILinkStateError, IncomingMessage, ReplyTarget
from .router import RouteKind, help_text, route_message, suggest_command
from .state_io import atomic_write_text


log = logging.getLogger(__name__)

_LOG_SECRET = re.compile(
    r"(?i)(authorization|token|context_token|aes[_-]?key|encrypt_query_param)"
    r'''["']?(?:\s*[=:]\s*|\s+)["']?(?:bearer\s+)?[^"'\s,;}]+'''
)
_WECHAT_ID = re.compile(r"[A-Za-z0-9_.-]+@im\.(?:wechat|bot)", re.IGNORECASE)
_OUTBOX_MAX_ATTEMPTS = 20
_OUTBOX_MAX_DEAD_LETTERS = 200
_OUTBOX_MAX_SENDS_PER_FLUSH = 20
_MAX_PENDING_CHATS = 100
_TYPING_REFRESH_SECONDS = 8.0


@dataclass(frozen=True)
class _OutboundItem:
    kind: str
    payload: str
    target: ReplyTarget
    item_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    job_id: str = ""
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str = ""
    failed_at: float = 0.0


@dataclass(frozen=True)
class _PendingChat:
    message_key: str
    session_key: str
    prompt: str
    conversation_title: str
    image_paths: tuple[str, ...]
    target: ReplyTarget


@dataclass(frozen=True)
class _ActiveJob:
    job_id: str
    message_key: str
    session_key: str
    operation: str
    target: ReplyTarget
    started_at: float


@dataclass(frozen=True)
class _CompletionEvent:
    text: str
    session_key: str | None
    image_paths: tuple[str, ...] = ()
    file_paths: tuple[str, ...] = ()


class BridgeApp:
    def __init__(
        self,
        config: AppConfig,
        *,
        event_sink: Callable[[ChatMessage], None] | None = None,
        state_sink: Callable[[str, str], None] | None = None,
        chat_runner: Any | None = None,
    ) -> None:
        self.config = config
        self._event_sink = event_sink
        self._state_sink = state_sink
        self._stop_event = threading.Event()
        self._stop_request_file = config.runtime_dir / "stop.request"
        self._process_started_at = time.time()
        self._stop_request_worker: threading.Thread | None = None
        self._manual_messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self._ack_backlog: deque[str] = deque()
        self._current_message_key: str | None = None
        self._current_send_index = 0
        self._reply_targets: dict[str, ReplyTarget] = {}
        self._project_selection_file = config.runtime_dir / "selected_projects.json"
        self._outbox_file = config.runtime_dir / "outbound_queue.json"
        self._dead_letter_file = config.runtime_dir / "outbound_dead_letters.json"
        self._active_jobs_file = config.runtime_dir / "active_jobs.json"
        self._completion_backlog: deque[_CompletionEvent] = deque()
        self._typing_targets: dict[str, ReplyTarget] = {}
        self._typing_last_attempt: dict[str, float] = {}
        self._pending_file = config.runtime_dir / "pending_chats.json"
        self._runtime_account_file = config.runtime_dir / "runtime_account.json"
        self.wechat = ILinkClient(
            credentials_path=config.ilink_credentials_file,
            state_path=config.ilink_state_file,
            response_prefix=config.response_prefix,
            max_reply_chars=config.max_reply_chars,
            long_poll_timeout_seconds=config.ilink_long_poll_timeout_seconds,
        )
        account_id = self.wechat.prepare_account()
        self._bind_runtime_account(account_id)
        self._selected_projects = self._load_selected_projects()
        self._outbox = self._load_outbox()
        self._dead_letters = self._load_outbound_file(self._dead_letter_file)
        self._active_jobs = self._load_active_jobs()
        self._pending_chats = self._load_pending_chats()
        self.runner = CodexRunner(
            codex_command=config.codex_command,
            runtime_dir=config.runtime_dir,
            work_timeout_seconds=config.work_timeout_seconds,
            projects=config.projects,
        )
        self.chat_runner = chat_runner or ChatGPTRunner(
            browser_channel=config.chatgpt_browser_channel,
            headless=config.chatgpt_headless,
            proxy_server=config.chatgpt_proxy_server,
            timeout_seconds=config.chat_timeout_seconds,
            runtime_dir=config.runtime_dir,
        )
        self._recover_active_jobs()

    def run(self) -> None:
        self._stop_request_worker = threading.Thread(
            target=self._watch_stop_requests,
            name="bridge-stop-request",
            daemon=True,
        )
        self._stop_request_worker.start()
        try:
            self._emit_state("connecting", "正在启动 ChatGPT 后台工作器")
            self.chat_runner.start()
            while not self._stop_event.is_set():
                try:
                    self._emit_state("connecting", "正在连接微信 iLink Bot API")
                    self.wechat.connect()
                    log.info("已连接微信 iLink Bot：%s", self.wechat.account_id)
                    self._emit_state("running", "正在监听微信 Bot 消息")
                    self._listen()
                except KeyboardInterrupt:
                    log.info("收到退出信号")
                    self.stop()
                except ILinkStateError as exc:
                    log.error("iLink 本地状态需要人工处理：%s", exc)
                    self._emit_state("error", str(exc))
                    raise
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    log.warning("微信 iLink 连接中断，5 秒后重试：%s", exc)
                    self._emit_state("reconnecting", f"iLink 连接失败，正在重试：{exc}")
                    self.wechat.close()
                    self._stop_event.wait(5)
        finally:
            self._stop_event.set()
            try:
                self._persist_manual_messages()
            except Exception:
                log.exception("关闭时持久化 GUI 手工消息失败")
            try:
                self._flush_ack_backlog()
            except Exception:
                log.exception("关闭时保存消息确认失败；下次启动可能重新投递该消息")
            self._cancel_all_typing()
            self.wechat.close()
            self.chat_runner.close()
            self.runner.close()
            try:
                self._collect_completion_events()
                self._persist_completion_backlog()
            except Exception:
                log.exception("关闭时持久化最终任务结果失败；下次启动将报告中断状态")
            stop_request_worker = self._stop_request_worker
            if (
                stop_request_worker is not None
                and stop_request_worker is not threading.current_thread()
            ):
                stop_request_worker.join(timeout=1)
            self._emit_state("stopped", "已停止")

    def stop(self) -> None:
        self._stop_event.set()
        self._cancel_all_typing()
        self.wechat.close()
        self.chat_runner.stop()
        self.runner.stop()

    def _watch_stop_requests(self) -> None:
        while not self._stop_event.wait(0.25):
            if not self._consume_stop_request():
                continue
            log.info("收到后台停止请求，开始优雅关闭")
            self.stop()
            return

    def _consume_stop_request(self) -> bool:
        path = self._stop_request_file
        if not path.is_file():
            return False
        try:
            modified_at = path.stat().st_mtime
            requested_pid = path.read_text(encoding="ascii").strip()
            path.unlink()
        except OSError:
            log.warning("读取后台停止请求失败：%s", path, exc_info=True)
            return False
        if modified_at < self._process_started_at - 1:
            log.info("忽略启动前遗留的停止请求")
            return False
        if requested_pid and requested_pid != str(os.getpid()):
            log.info("忽略属于旧进程的停止请求：%s", requested_pid)
            return False
        return True

    def enqueue_message(self, text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            self._manual_messages.put((uuid.uuid4().hex, cleaned))

    def _emit_event(self, message: ChatMessage) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(message)
        except Exception:
            log.exception("界面消息回调失败")
            raise

    def _emit_state(self, state: str, text: str) -> None:
        if self._state_sink is None:
            return
        try:
            self._state_sink(state, text)
        except Exception:
            log.exception("界面状态回调失败")

    def _start_typing(self, session_key: str, target: ReplyTarget) -> None:
        self._typing_targets[session_key] = target
        self._send_typing(session_key, target, active=True)

    def _stop_typing(self, session_key: str) -> None:
        target = self._typing_targets.pop(session_key, None)
        self._typing_last_attempt.pop(session_key, None)
        if target is not None:
            self._send_typing(session_key, target, active=False)

    def _send_typing(
        self,
        session_key: str,
        target: ReplyTarget,
        *,
        active: bool,
    ) -> None:
        try:
            self.wechat.set_typing(active, target)
        except Exception as exc:
            action = "显示" if active else "取消"
            log.warning("%s微信输入状态失败，继续处理消息：%s", action, exc)
        finally:
            if active and session_key in self._typing_targets:
                self._typing_last_attempt[session_key] = time.monotonic()

    def _refresh_typing(self) -> None:
        now = time.monotonic()
        for session_key, target in list(self._typing_targets.items()):
            last_attempt = self._typing_last_attempt.get(session_key, 0.0)
            if now - last_attempt >= _TYPING_REFRESH_SECONDS:
                self._send_typing(session_key, target, active=True)

    def _cancel_all_typing(self) -> None:
        for session_key in list(self._typing_targets):
            self._stop_typing(session_key)

    def _send(self, text: str, target: ReplyTarget) -> None:
        client_id: str | None = None
        if self._current_message_key is not None:
            digest = hashlib.sha256(
                self._current_message_key.encode("utf-8")
            ).hexdigest()[:24]
            client_id = f"inbound:{digest}:reply:{self._current_send_index}"
            self._current_send_index += 1
        self.wechat.send(text, target, client_id=client_id)
        self._emit_event(
            ChatMessage.create(
                message_id=f"outgoing:{client_id or uuid.uuid4().hex}",
                kind="outgoing",
                sender="微信 Bot",
                peer_id=target.user_id,
                text=text,
            )
        )

    def _send_event(
        self,
        text: str,
        session_key: str | None,
        image_paths: tuple[str, ...] = (),
        file_paths: tuple[str, ...] = (),
        *,
        target: ReplyTarget | None = None,
        job_id: str = "",
        event_id: str = "",
    ) -> None:
        """Durably enqueue an asynchronous result before attempting delivery."""
        self._queue_event(
            text,
            session_key,
            image_paths,
            file_paths,
            target=target,
            job_id=job_id,
            event_id=event_id,
        )
        self._flush_outbox()

    def _queue_event(
        self,
        text: str,
        session_key: str | None,
        image_paths: tuple[str, ...] = (),
        file_paths: tuple[str, ...] = (),
        *,
        target: ReplyTarget | None = None,
        job_id: str = "",
        event_id: str = "",
    ) -> None:
        target = target or self._reply_targets.get(session_key or "")
        if target is None:
            try:
                target = self.wechat.default_target()
            except Exception:
                log.error("异步回复缺少可用的 iLink 目标，已丢弃：%s", text)
                return
        items: list[_OutboundItem] = []
        stable_id = job_id or event_id
        if text.strip():
            item_id = f"{stable_id}:text" if stable_id else uuid.uuid4().hex
            items.append(
                _OutboundItem("text", text, target, item_id=item_id, job_id=job_id)
            )
        for index, image_path in enumerate(image_paths):
            item_id = (
                f"{stable_id}:image:{index}" if stable_id else uuid.uuid4().hex
            )
            items.append(
                _OutboundItem(
                    "image", image_path, target, item_id=item_id, job_id=job_id
                )
            )
        for index, file_path in enumerate(file_paths):
            item_id = (
                f"{stable_id}:file:{index}" if stable_id else uuid.uuid4().hex
            )
            items.append(
                _OutboundItem(
                    "file", file_path, target, item_id=item_id, job_id=job_id
                )
            )
        existing_ids = {
            item.item_id for item in (*self._outbox, *self._dead_letters)
        }
        items = [item for item in items if item.item_id not in existing_ids]
        if not items:
            return
        original_length = len(self._outbox)
        self._outbox.extend(items)
        try:
            self._save_outbox()
        except Exception:
            del self._outbox[original_length:]
            raise

    def _load_outbox(self) -> list[_OutboundItem]:
        return self._load_outbound_file(self._outbox_file)

    @staticmethod
    def _load_outbound_file(path: Path) -> list[_OutboundItem]:
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("顶层不是 JSON 数组")
            result: list[_OutboundItem] = []
            for index, value in enumerate(raw):
                if not isinstance(value, dict):
                    raise ValueError(f"第 {index + 1} 项不是 JSON 对象")
                kind = str(value.get("kind") or "")
                payload = str(value.get("payload") or "")
                user_id = str(value.get("user_id") or "")
                if kind not in {"text", "image", "file"} or not payload or not user_id:
                    raise ValueError(f"第 {index + 1} 项缺少有效类型、内容或用户")
                attempts = int(value.get("attempts") or 0)
                next_attempt_at = float(value.get("next_attempt_at") or 0.0)
                failed_at = float(value.get("failed_at") or 0.0)
                if (
                    attempts < 0
                    or not math.isfinite(next_attempt_at)
                    or next_attempt_at < 0
                    or not math.isfinite(failed_at)
                    or failed_at < 0
                ):
                    raise ValueError(f"第 {index + 1} 项重试状态无效")
                raw_item_id = value.get("item_id")
                item_id = (
                    raw_item_id.strip()
                    if isinstance(raw_item_id, str)
                    else ""
                )
                if not item_id:
                    legacy_payload = json.dumps(
                        {
                            "kind": kind,
                            "payload": payload,
                            "user_id": user_id,
                            "context_token": str(
                                value.get("context_token") or ""
                            ),
                            "job_id": str(value.get("job_id") or ""),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    digest = hashlib.sha256(
                        legacy_payload.encode("utf-8")
                    ).hexdigest()[:24]
                    item_id = f"legacy:{path.name}:{index}:{digest}"
                result.append(
                    _OutboundItem(
                        kind,
                        payload,
                        ReplyTarget(user_id, str(value.get("context_token") or "")),
                        item_id=item_id,
                        job_id=str(value.get("job_id") or ""),
                        attempts=attempts,
                        next_attempt_at=next_attempt_at,
                        last_error=str(value.get("last_error") or "")[-1000:],
                        failed_at=failed_at,
                    )
                )
            return result
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"持久化出站队列无法读取，已停止以避免覆盖 {path}: {exc}") from exc

    def _save_outbox(self) -> None:
        self._save_outbound_file(self._outbox_file, self._outbox)

    def _save_dead_letters(self) -> None:
        self._save_outbound_file(self._dead_letter_file, self._dead_letters)

    def _save_outbound_file(self, path: Path, items: list[_OutboundItem]) -> None:
        payload = [
            {
                "kind": item.kind,
                "payload": item.payload,
                "user_id": item.target.user_id,
                "context_token": item.target.context_token,
                "item_id": item.item_id,
                "job_id": item.job_id,
                "attempts": item.attempts,
                "next_attempt_at": item.next_attempt_at,
                "last_error": item.last_error,
                "failed_at": item.failed_at,
            }
            for item in items
        ]
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _flush_outbox(self) -> None:
        if not self._outbox:
            return
        now = time.time()
        original_outbox = self._outbox
        original_dead_letters = self._dead_letters
        remaining: list[_OutboundItem] = []
        dead_letters = list(self._dead_letters)
        dead_letter_ids = {item.item_id for item in dead_letters}
        dead_letters_added = False
        changed = False
        attempted = 0
        for index, item in enumerate(self._outbox):
            if item.next_attempt_at > now:
                remaining.append(item)
                continue
            if attempted >= _OUTBOX_MAX_SENDS_PER_FLUSH:
                remaining.extend(self._outbox[index:])
                break
            attempted += 1
            try:
                if item.kind == "text":
                    self.wechat.send(
                        item.payload,
                        item.target,
                        client_id=item.item_id,
                    )
                    self._emit_event(
                        ChatMessage.create(
                            message_id=f"outgoing:{item.item_id}",
                            kind="outgoing",
                            sender="微信 Bot",
                            peer_id=item.target.user_id,
                            text=item.payload,
                        )
                    )
                elif item.kind == "image":
                    self.wechat.send_image(
                        item.payload,
                        item.target,
                        client_id=item.item_id,
                    )
                    self._emit_event(
                        ChatMessage.create(
                            message_id=f"outgoing:{item.item_id}",
                            kind="outgoing",
                            sender="微信 Bot",
                            peer_id=item.target.user_id,
                            text="[图片]",
                            content_type="image",
                        )
                    )
                else:
                    self.wechat.send_file(
                        item.payload,
                        item.target,
                        client_id=item.item_id,
                    )
                    self._emit_event(
                        ChatMessage.create(
                            message_id=f"outgoing:{item.item_id}",
                            kind="outgoing",
                            sender="微信 Bot",
                            peer_id=item.target.user_id,
                            text=f"[文件] {Path(item.payload).name}",
                            content_type="file",
                        )
                    )
            except ChatHistoryError as exc:
                remaining.append(
                    replace(
                        item,
                        next_attempt_at=now + 5.0,
                        last_error=str(exc)[-1000:],
                    )
                )
                remaining.extend(self._outbox[index + 1 :])
                changed = True
                log.error(
                    "出站消息已投递，但历史记录尚未落盘；保留队列并稍后幂等重试：%s",
                    exc,
                )
                break
            except Exception as exc:
                attempts = item.attempts + 1
                detail = f"{type(exc).__name__}: {exc}"[-1000:]
                permanent = self._permanent_outbox_failure(item, exc)
                if permanent or attempts >= _OUTBOX_MAX_ATTEMPTS:
                    failed = replace(
                        item,
                        attempts=attempts,
                        last_error=detail,
                        failed_at=now,
                    )
                    if failed.item_id not in dead_letter_ids:
                        dead_letters.append(failed)
                        dead_letters = dead_letters[-_OUTBOX_MAX_DEAD_LETTERS:]
                        dead_letter_ids = {entry.item_id for entry in dead_letters}
                        dead_letters_added = True
                    log.error(
                        "异步出站项目已移入死信队列（%s，尝试 %d 次）：%s",
                        item.item_id,
                        attempts,
                        detail,
                    )
                else:
                    delay = min(300.0, 2.0 ** (attempts - 1))
                    remaining.append(
                        replace(
                            item,
                            attempts=attempts,
                            next_attempt_at=now + delay,
                            last_error=detail,
                        )
                    )
                    log.warning(
                        "异步出站发送失败，%.0f 秒后重试（%d/%d）：%s",
                        delay,
                        attempts,
                        _OUTBOX_MAX_ATTEMPTS,
                        detail,
                    )
                changed = True
                continue
            changed = True
        if dead_letters_added:
            self._dead_letters = dead_letters
            try:
                self._save_dead_letters()
            except Exception:
                self._dead_letters = original_dead_letters
                raise
            self._emit_event(
                ChatMessage.create(
                    message_id=f"system:{uuid.uuid4().hex}",
                    kind="system",
                    sender="系统",
                    peer_id="",
                    text=(
                        f"有异步回复发送失败并进入死信队列；当前共 {len(dead_letters)} 条，"
                        "可发送 @健康检查 查看详情"
                    ),
                )
            )
        if changed:
            self._outbox = remaining
            try:
                self._save_outbox()
            except Exception:
                # Stable client IDs make replay safe; retain the old durable
                # queue in memory so a transient disk error cannot lose work.
                self._outbox = original_outbox
                raise

    @staticmethod
    def _permanent_outbox_failure(item: _OutboundItem, error: BaseException) -> bool:
        if item.kind in {"image", "file"} and not Path(item.payload).is_file():
            return True
        detail = str(error)
        return any(
            marker in detail
            for marker in ("不存在", "超过 20 MB", "内容为空", "缺少文件名")
        )

    def _load_active_jobs(self) -> dict[str, _ActiveJob]:
        if not self._active_jobs_file.is_file():
            return {}
        try:
            raw = json.loads(self._active_jobs_file.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("顶层不是 JSON 数组")
            result: dict[str, _ActiveJob] = {}
            for index, value in enumerate(raw):
                if not isinstance(value, dict):
                    raise ValueError(f"第 {index + 1} 项不是 JSON 对象")
                session_key = str(value.get("session_key") or "")
                message_key = str(value.get("message_key") or "")
                user_id = str(value.get("user_id") or "")
                if not session_key or not message_key or not user_id:
                    raise ValueError(f"第 {index + 1} 项缺少会话、消息或用户")
                started_at = float(value.get("started_at") or 0.0)
                if not math.isfinite(started_at) or started_at < 0:
                    raise ValueError(f"第 {index + 1} 项开始时间无效")
                result[session_key] = _ActiveJob(
                    job_id=str(value.get("job_id") or uuid.uuid4().hex),
                    message_key=message_key,
                    session_key=session_key,
                    operation=str(value.get("operation") or "异步任务"),
                    target=ReplyTarget(
                        user_id,
                        str(value.get("context_token") or ""),
                    ),
                    started_at=started_at,
                )
            return result
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"活动任务记录无法读取，已停止以避免重复执行 {self._active_jobs_file}: {exc}"
            ) from exc

    def _save_active_jobs(self) -> None:
        payload = [
            {
                "job_id": job.job_id,
                "message_key": job.message_key,
                "session_key": job.session_key,
                "operation": job.operation,
                "user_id": job.target.user_id,
                "context_token": job.target.context_token,
                "started_at": job.started_at,
            }
            for job in self._active_jobs.values()
        ]
        atomic_write_text(
            self._active_jobs_file,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _stage_active_job(
        self,
        message_key: str,
        session_key: str,
        operation: str,
        target: ReplyTarget,
    ) -> _ActiveJob:
        existing = self._active_jobs.get(session_key)
        if existing is not None and existing.message_key == message_key:
            return existing
        job = _ActiveJob(
            job_id=uuid.uuid4().hex,
            message_key=message_key,
            session_key=session_key,
            operation=operation,
            target=target,
            started_at=time.time(),
        )
        self._active_jobs[session_key] = job
        try:
            self._save_active_jobs()
        except Exception:
            if existing is None:
                self._active_jobs.pop(session_key, None)
            else:
                self._active_jobs[session_key] = existing
            raise
        return job

    def _discard_active_job(self, session_key: str, job_id: str) -> None:
        current = self._active_jobs.get(session_key)
        if current is None or current.job_id != job_id:
            return
        self._active_jobs.pop(session_key, None)
        try:
            self._save_active_jobs()
        except Exception:
            self._active_jobs[session_key] = current
            raise

    def _queue_async_result(
        self,
        text: str,
        session_key: str | None,
        image_paths: tuple[str, ...] = (),
        file_paths: tuple[str, ...] = (),
    ) -> None:
        job = self._active_jobs.get(session_key or "")
        already_durable = bool(
            job and any(item.job_id == job.job_id for item in self._outbox)
        )
        if not already_durable:
            self._queue_event(
                text,
                session_key,
                image_paths,
                file_paths,
                target=job.target if job else None,
                job_id=job.job_id if job else "",
            )
        if job is not None:
            # Repair a stale pending snapshot if claiming this job succeeded but
            # removing it from pending storage failed before the worker finished.
            self._pending_chats = [
                item
                for item in self._pending_chats
                if item.message_key != job.message_key
            ]
            self._save_pending_chats()
            self._stop_typing(job.session_key)
            self._discard_active_job(job.session_key, job.job_id)
        elif session_key:
            self._stop_typing(session_key)

    def _recover_active_jobs(self) -> None:
        if not self._active_jobs:
            return
        claimed_message_keys = {
            job.message_key for job in self._active_jobs.values() if job.message_key
        }
        remaining_pending = [
            item
            for item in self._pending_chats
            if item.message_key not in claimed_message_keys
        ]
        if len(remaining_pending) != len(self._pending_chats):
            self._pending_chats = remaining_pending
            self._save_pending_chats()
        durable_job_ids = {item.job_id for item in self._outbox if item.job_id}
        added = False
        for job in self._active_jobs.values():
            if job.job_id in durable_job_ids:
                continue
            detail = (
                f"上一次{job.operation}因程序中断，完成状态未知。"
                "请先确认当前状态，再决定是否重试。"
            )
            self._outbox.append(
                _OutboundItem(
                    "text",
                    detail,
                    job.target,
                    job_id=job.job_id,
                )
            )
            added = True
        if added:
            self._save_outbox()
        for job in self._active_jobs.values():
            # The async job journal takes ownership before iLink acknowledge.
            # Mark the original inbound key handled so a crash in that narrow
            # window cannot start the same side-effecting task again.
            self.wechat.acknowledge(job.message_key)
        self._active_jobs.clear()
        self._save_active_jobs()

    def _collect_completion_events(self) -> None:
        for event in self.runner.drain_events():
            self._completion_backlog.append(
                _CompletionEvent(event.text, event.session_key)
            )
        for event in self.chat_runner.drain_events():
            self._completion_backlog.append(
                _CompletionEvent(
                    event.text,
                    event.session_key,
                    event.image_paths,
                    event.file_paths,
                )
            )

    def _persist_completion_backlog(self) -> None:
        while self._completion_backlog:
            event = self._completion_backlog[0]
            self._queue_async_result(
                event.text,
                event.session_key,
                event.image_paths,
                event.file_paths,
            )
            self._completion_backlog.popleft()

    def _process_incoming_batch(self, messages: list[IncomingMessage]) -> None:
        for index, message in enumerate(messages):
            try:
                self._process_incoming(message)
            except Exception:
                for unprocessed in messages[index:]:
                    self.wechat.retry(unprocessed)
                raise
            try:
                self.wechat.acknowledge(message.key)
            except Exception:
                if message.key not in self._ack_backlog:
                    self._ack_backlog.append(message.key)
                for unprocessed in messages[index + 1 :]:
                    self.wechat.retry(unprocessed)
                raise

    def _flush_ack_backlog(self) -> None:
        while self._ack_backlog:
            self.wechat.acknowledge(self._ack_backlog[0])
            self._ack_backlog.popleft()

    def _persist_manual_messages(self) -> None:
        while True:
            try:
                event_id, text = self._manual_messages.get_nowait()
            except queue.Empty:
                return
            try:
                self._queue_event(
                    text,
                    None,
                    target=self.wechat.default_target(),
                    event_id=f"manual:{event_id}",
                )
            except Exception:
                self._manual_messages.put((event_id, text))
                raise

    def _listen(self) -> None:
        consecutive_errors = 0
        while not self._stop_event.is_set():
            try:
                self._persist_manual_messages()
                self._flush_ack_backlog()
                self._collect_completion_events()
                self._persist_completion_backlog()
                self._flush_outbox()
                self._start_next_pending_chat()
                self._refresh_typing()

                self._process_incoming_batch(self.wechat.poll())
                consecutive_errors = 0
            except KeyboardInterrupt:
                raise
            except ILinkError:
                raise
            except Exception:
                consecutive_errors += 1
                log.exception("微信消息处理或发送失败（%d/5）", consecutive_errors)
                if consecutive_errors >= 5:
                    raise RuntimeError("微信消息连续 5 次处理失败，准备重新连接")
            self._stop_event.wait(self.config.poll_seconds)

    def _is_allowed(self, sender_id: str) -> bool:
        allowed = self.config.ilink_allowed_user_ids
        return sender_id in allowed if allowed else sender_id == self.wechat.user_id

    def _can_run_codex(self, sender_id: str) -> bool:
        allowed = self.config.ilink_codex_user_ids
        return sender_id in allowed if allowed else sender_id == self.wechat.user_id

    def _process_incoming(self, message: IncomingMessage) -> None:
        previous_key = self._current_message_key
        previous_index = self._current_send_index
        self._current_message_key = message.key
        self._current_send_index = 0
        try:
            if not self._is_allowed(message.sender_id):
                log.warning("忽略未授权的 iLink 消息发送者：%s", message.sender_id)
                return
            target = message.reply_target
            if message.image_item is not None:
                try:
                    message = self.wechat.materialize_image(message)
                except ILinkError as exc:
                    log.warning("接收微信图片失败：%s", exc)
                    self._send(f"图片接收失败，请重新发送（{exc}）", target)
                    return
            log.info("收到 iLink 消息（%s）：%s", message.sender_id, message.content)
            self._emit_event(
                ChatMessage.create(
                    message_id=self._inbound_event_id(message.key, "gui-message"),
                    kind="incoming",
                    sender=message.sender,
                    peer_id=message.sender_id,
                    text=message.content,
                    content_type="image" if message.image_path else "text",
                )
            )
            session_key = self._chat_session_key(message)
            self._reply_targets[session_key] = target
            self._acknowledge(message, target)
            self._handle(message, target, session_key)
        finally:
            self._current_message_key = previous_key
            self._current_send_index = previous_index

    def _acknowledge(self, message: IncomingMessage, target: ReplyTarget) -> None:
        if not self.config.send_received_ack or message.content.lstrip().startswith("@"):
            return
        try:
            self._send(self.config.received_ack_text, target)
        except Exception:
            log.exception("发送收到确认失败，继续处理原消息")

    def _begin_tracked_job(
        self,
        *,
        message_key: str,
        session_key: str,
        operation: str,
        target: ReplyTarget,
        starter: Callable[[], tuple[bool, str]],
    ) -> tuple[bool, str, bool]:
        existing = self._active_jobs.get(session_key)
        if existing is not None:
            if existing.message_key == message_key:
                return True, "任务已被持久化接管", False
            return False, "当前会话已有任务正在等待完成", False
        job = self._stage_active_job(message_key, session_key, operation, target)
        try:
            ok, response = starter()
        except Exception:
            self._discard_active_job(session_key, job.job_id)
            raise
        if not ok:
            self._discard_active_job(session_key, job.job_id)
        else:
            self._start_typing(session_key, target)
        return ok, response, ok

    def _handle(
        self,
        message: IncomingMessage,
        target: ReplyTarget,
        session_key: str,
    ) -> None:
        route = route_message(message.content)
        codex_commands = {
            RouteKind.WORK,
            RouteKind.CONTINUE,
            RouteKind.STATUS,
            RouteKind.STOP,
            RouteKind.CACHE_CLEAR,
            RouteKind.RECENT_TASKS,
            RouteKind.PROJECT_LIST,
            RouteKind.SWITCH_PROJECT,
            RouteKind.VIEW_LOGS,
            RouteKind.RECONNECT_WECHAT,
            RouteKind.RESTART_BROWSER,
        }
        if route.kind in codex_commands and not self._can_run_codex(message.sender_id):
            log.warning("拒绝未授权的 Codex 请求：%s", message.sender_id)
            self._send("无权限执行 Codex 操作", target)
            return
        if route.kind == RouteKind.UNKNOWN_COMMAND:
            suggestion = suggest_command(route.prompt)
            suggestion_text = f"\n你是否想使用：{suggestion}" if suggestion else ""
            self._send(
                f"命令不存在：{route.prompt}{suggestion_text}"
                "\n发送 @帮助 查看全部命令。",
                target,
            )
            return
        if route.kind == RouteKind.HELP:
            self._send(help_text(list(self.config.projects), self.config.default_project), target)
            return
        if route.kind == RouteKind.STATUS:
            status = (
                self.chat_runner.status()
                if self.chat_runner.active
                else self.runner.status(session_key)
            )
            if self._pending_chats:
                status += f"\n待处理消息：{len(self._pending_chats)} 条"
            self._send(status, target)
            return
        if route.kind == RouteKind.STOP:
            result = self.chat_runner.stop() if self.chat_runner.active else self.runner.stop()
            self._send(result, target)
            return
        if route.kind == RouteKind.CURRENT_CHAT:
            self._send(
                self.chat_runner.conversation_info(
                    session_key,
                    self.config.chatgpt_conversation_title,
                ),
                target,
            )
            return
        if route.kind == RouteKind.CONVERSATION_LIST:
            self._send(
                self.chat_runner.conversation_list(
                    session_key,
                    self.config.chatgpt_conversation_title,
                ),
                target,
            )
            return
        if route.kind == RouteKind.SWITCH_CHAT:
            ok, response = self.chat_runner.switch_conversation(
                session_key,
                route.number or 0,
                self.config.chatgpt_conversation_title,
            )
            self._send(response, target)
            return
        if route.kind == RouteKind.ARCHIVE_CHAT:
            ok, response, _ = self._begin_tracked_job(
                message_key=message.key,
                session_key=session_key,
                operation="归档 ChatGPT 对话",
                target=target,
                starter=lambda: self.chat_runner.begin_archive(session_key),
            )
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.EXPORT_CHAT:
            ok, response, _ = self._begin_tracked_job(
                message_key=message.key,
                session_key=session_key,
                operation="导出 ChatGPT 对话",
                target=target,
                starter=lambda: self.chat_runner.begin_export(
                    session_key,
                    self.config.chatgpt_conversation_title,
                ),
            )
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.SUMMARIZE_CHAT:
            ok, response, _ = self._begin_tracked_job(
                message_key=message.key,
                session_key=session_key,
                operation="总结 ChatGPT 对话",
                target=target,
                starter=lambda: self.chat_runner.begin_summary(session_key),
            )
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.RENAME_CHAT:
            ok, response, _ = self._begin_tracked_job(
                message_key=message.key,
                session_key=session_key,
                operation="重命名 ChatGPT 对话",
                target=target,
                starter=lambda: self.chat_runner.begin_rename(
                    session_key, route.prompt
                ),
            )
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.RETRY:
            ok, response, _ = self._begin_tracked_job(
                message_key=message.key,
                session_key=session_key,
                operation="重试 ChatGPT 对话",
                target=target,
                starter=lambda: self.chat_runner.begin_retry(session_key),
            )
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.RESEND:
            if self.chat_runner.active or self.runner.active:
                self._send("当前有任务正在执行，请完成或发送 @停止 后再重发", target)
                return
            event = self.chat_runner.last_reply(session_key)
            if event is None or (
                not event.text.strip()
                and not event.image_paths
                and not event.file_paths
            ):
                self._send("当前没有可以重发的成功回复", target)
                return
            self._send_event(
                event.text,
                session_key,
                event.image_paths,
                event.file_paths,
                event_id=self._inbound_event_id(message.key, "resend"),
            )
            return
        if route.kind == RouteKind.RECENT_TASKS:
            self._send(self.runner.recent_tasks(session_key), target)
            return
        if route.kind == RouteKind.PROJECT_LIST:
            self._send(self._project_list(session_key), target)
            return
        if route.kind == RouteKind.SWITCH_PROJECT:
            self._send(
                self._switch_project(session_key, route.project or ""),
                target,
            )
            return
        if route.kind == RouteKind.CACHE_STATUS:
            self._send(self._cache_status(), target)
            return
        if route.kind == RouteKind.CACHE_CLEAR:
            days = route.days if route.days is not None else 7
            if not 1 <= days <= 3650:
                self._send("缓存保留天数必须在 1 到 3650 天之间", target)
                return
            self._send(self._clear_cache(days), target)
            return
        if route.kind == RouteKind.DOCTOR:
            self._send(self._doctor_status(), target)
            return
        if route.kind == RouteKind.VIEW_LOGS:
            number = route.number if route.number is not None else 50
            if not 1 <= number <= 200:
                self._send("日志条数必须在 1 到 200 之间", target)
                return
            self._send(self._recent_logs(number), target)
            return
        if route.kind == RouteKind.RECONNECT_WECHAT:
            if self.chat_runner.active or self.runner.active:
                self._send("当前有任务正在执行，请完成或发送 @停止 后再重连", target)
                return
            try:
                self.wechat.reconnect()
            except Exception:
                log.exception("微信 iLink 手动重连失败")
                raise
            self._send("微信 iLink 已重新连接", target)
            return
        if route.kind == RouteKind.RESTART_BROWSER:
            ok, response = self.chat_runner.restart()
            self._send(response, target)
            return
        if route.kind == RouteKind.NEW_CHAT:
            if self.runner.active:
                self._send("Codex 任务正在执行，请发送 @状态 或 @停止", target)
                return
            ok, response, _ = self._begin_tracked_job(
                message_key=message.key,
                session_key=session_key,
                operation="切换 ChatGPT 新对话",
                target=target,
                starter=lambda: self.chat_runner.begin_reset(session_key),
            )
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.CONTINUE:
            if self.chat_runner.active:
                self._send("ChatGPT 请求正在执行，请发送 @状态 或 @停止", target)
                return
            ok, response, _ = self._begin_tracked_job(
                message_key=message.key,
                session_key=session_key,
                operation="继续 Codex 任务",
                target=target,
                starter=lambda: self.runner.begin_continue(
                    route.prompt, session_key
                ),
            )
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.WORK:
            if self.chat_runner.active:
                self._send("ChatGPT 请求正在执行，请发送 @状态 或 @停止", target)
                return
            project = route.project or self._selected_project(session_key)
            project_path = self.config.projects.get(project)
            if project_path is None:
                self._send(
                    f"未知项目 {project!r}；可用项目：{'、'.join(self.config.projects)}",
                    target,
                )
                return
            ok, response, _ = self._begin_tracked_job(
                message_key=message.key,
                session_key=session_key,
                operation=f"Codex 任务（{project}）",
                target=target,
                starter=lambda: self.runner.begin_work(
                    project, project_path, route.prompt, session_key
                ),
            )
            if not ok:
                self._send(response, target)
            return

        if self.runner.active:
            self._send("Codex 任务正在执行，请发送 @状态 或 @停止", target)
            return
        prompt = route.prompt
        if message.image_path and prompt == "[图片]":
            prompt = "请分析这张图片，并说明你看到了什么。"
        pending = _PendingChat(
            message_key=message.key,
            session_key=session_key,
            prompt=prompt,
            conversation_title=self._chat_conversation_title(message),
            image_paths=(message.image_path,) if message.image_path else (),
            target=target,
        )
        if self.chat_runner.active or self._pending_chats:
            queued = self._enqueue_pending_chat(pending)
            if queued and not self.chat_runner.active:
                self._start_next_pending_chat()
            return
        ok, response, _ = self._begin_tracked_job(
            message_key=message.key,
            session_key=session_key,
            operation="ChatGPT 请求",
            target=target,
            starter=lambda: self.chat_runner.begin_chat(
                session_key,
                prompt,
                conversation_title=pending.conversation_title,
                image_paths=pending.image_paths,
            ),
        )
        if not ok:
            if response.startswith("已有 ChatGPT 请求在执行"):
                queued = self._enqueue_pending_chat(pending)
                if queued and not self.chat_runner.active:
                    self._start_next_pending_chat()
            else:
                self._send(response, target)

    def _enqueue_pending_chat(self, pending: _PendingChat) -> bool:
        if any(item.message_key == pending.message_key for item in self._pending_chats):
            return True
        if len(self._pending_chats) >= _MAX_PENDING_CHATS:
            self._send(
                "待处理消息已达到 100 条上限，请稍后再发送",
                pending.target,
            )
            return False
        self._pending_chats.append(pending)
        try:
            self._save_pending_chats()
        except Exception:
            self._pending_chats.pop()
            raise
        self._send(
            f"当前请求仍在处理；本消息已排队（第 {len(self._pending_chats)} 位）",
            pending.target,
        )
        return True

    def _start_next_pending_chat(self) -> None:
        if not self._pending_chats or self.chat_runner.active or self.runner.active:
            return
        pending = self._pending_chats[0]
        self._reply_targets[pending.session_key] = pending.target
        ok, response, started = self._begin_tracked_job(
            message_key=pending.message_key,
            session_key=pending.session_key,
            operation="ChatGPT 排队请求",
            target=pending.target,
            starter=lambda: self.chat_runner.begin_chat(
                pending.session_key,
                pending.prompt,
                conversation_title=pending.conversation_title,
                image_paths=pending.image_paths,
            ),
        )
        if not ok and response.startswith("已有 ChatGPT 请求在执行"):
            return
        if not ok:
            self._queue_event(
                response,
                pending.session_key,
                target=pending.target,
                event_id=self._inbound_event_id(
                    pending.message_key,
                    "pending-error",
                ),
            )
        if started or not ok:
            removed = self._pending_chats.pop(0)
            try:
                self._save_pending_chats()
            except Exception:
                self._pending_chats.insert(0, removed)
                raise
        if not ok:
            self._flush_outbox()

    def _load_pending_chats(self) -> list[_PendingChat]:
        if not self._pending_file.is_file():
            return []
        try:
            raw = json.loads(self._pending_file.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("顶层不是 JSON 数组")
            result: list[_PendingChat] = []
            for index, value in enumerate(raw):
                if not isinstance(value, dict):
                    raise ValueError(f"第 {index + 1} 项不是 JSON 对象")
                message_key = str(value.get("message_key") or "")
                session_key = str(value.get("session_key") or "")
                prompt = str(value.get("prompt") or "")
                user_id = str(value.get("user_id") or "")
                if not message_key or not session_key or not prompt or not user_id:
                    raise ValueError(f"第 {index + 1} 项缺少消息、会话、提示或用户")
                raw_images = value.get("image_paths")
                if raw_images is not None and not isinstance(raw_images, list):
                    raise ValueError(f"第 {index + 1} 项图片路径不是数组")
                image_paths = tuple(
                    str(path) for path in (raw_images or []) if str(path)
                )
                result.append(
                    _PendingChat(
                        message_key,
                        session_key,
                        prompt,
                        str(value.get("conversation_title") or "微信助手"),
                        image_paths,
                        ReplyTarget(
                            user_id,
                            str(value.get("context_token") or ""),
                        ),
                    )
                )
            return result
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"待处理消息队列无法读取，已停止以避免丢失 {self._pending_file}: {exc}"
            ) from exc

    def _save_pending_chats(self) -> None:
        payload = [
            {
                "message_key": item.message_key,
                "session_key": item.session_key,
                "prompt": item.prompt,
                "conversation_title": item.conversation_title,
                "image_paths": list(item.image_paths),
                "user_id": item.target.user_id,
                "context_token": item.target.context_token,
            }
            for item in self._pending_chats
        ]
        atomic_write_text(
            self._pending_file,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    @staticmethod
    def _chat_session_key(message: IncomingMessage) -> str:
        return f"ilink:{message.account_id}:{message.sender_id}"

    @staticmethod
    def _inbound_event_id(message_key: str, operation: str) -> str:
        digest = hashlib.sha256(message_key.encode("utf-8")).hexdigest()[:24]
        return f"inbound:{digest}:{operation}"

    def _chat_conversation_title(self, message: IncomingMessage) -> str:
        return self.config.chatgpt_conversation_title

    def _bind_runtime_account(self, account_id: str) -> None:
        path = self._runtime_account_file
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("顶层不是 JSON 对象")
                stored = str(raw.get("account_id") or "")
                if not stored:
                    raise ValueError("缺少 account_id")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ILinkStateError(
                    f"运行状态账号绑定无法读取，已停止以避免覆盖 {path}: {exc}"
                ) from exc
            if stored != account_id:
                raise ILinkStateError(
                    "运行状态属于其他 iLink Bot，已停止以避免跨账号发送："
                    f"state={stored!r}, credentials={account_id!r}。"
                    f"请先备份并移走 {self.config.runtime_dir} 中的消息与任务状态"
                )
            return
        atomic_write_text(
            path,
            json.dumps(
                {"version": 1, "account_id": account_id},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def _load_selected_projects(self) -> dict[str, str]:
        if not self._project_selection_file.is_file():
            return {}
        try:
            raw = json.loads(
                self._project_selection_file.read_text(encoding="utf-8")
            )
            if not isinstance(raw, dict):
                raise ValueError("顶层不是 JSON 对象")
            return {
                str(key): str(value)
                for key, value in raw.items()
                if str(key) and str(value) in self.config.projects
            }
        except Exception as exc:
            log.warning("忽略无法读取的项目选择记录：%s", exc)
            return {}

    def _save_selected_projects(self) -> None:
        atomic_write_text(
            self._project_selection_file,
            json.dumps(self._selected_projects, ensure_ascii=False, indent=2) + "\n",
        )

    def _selected_project(self, session_key: str) -> str:
        return self._selected_projects.get(session_key, self.config.default_project)

    def _project_list(self, session_key: str) -> str:
        selected = self._selected_project(session_key)
        lines = ["可用 Codex 项目："]
        for name in self.config.projects:
            marker = " [当前]" if name == selected else ""
            lines.append(f"- {name}{marker}")
        lines.append("发送 @切换项目：项目名 进行切换")
        return "\n".join(lines)

    def _switch_project(self, session_key: str, project: str) -> str:
        if self.runner.active:
            return "Codex 任务正在执行，暂不能切换项目"
        if project not in self.config.projects:
            return f"未知项目 {project!r}；可用项目：{'、'.join(self.config.projects)}"
        self._selected_projects[session_key] = project
        self._save_selected_projects()
        return f"当前 Codex 项目已切换为：{project}"

    @staticmethod
    def _sanitize_log_line(line: str) -> str:
        cleaned = _LOG_SECRET.sub(lambda match: f"{match.group(1)}=[已隐藏]", line)
        return _WECHAT_ID.sub("[微信ID]", cleaned)[:600]

    def _recent_logs(self, number: int) -> str:
        candidates = []
        for name in ("bridge.log", "bridge.err.log", "gui.log", "bridge.out.log"):
            path = (self.config.runtime_dir / name).resolve()
            try:
                if path.is_file() and path.stat().st_size:
                    candidates.append(path)
            except OSError:
                continue
        if not candidates:
            return "当前没有可读取的运行日志"
        path = max(candidates, key=lambda candidate: candidate.stat().st_mtime)
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - 512 * 1024), os.SEEK_SET)
                payload = stream.read()
            lines = payload.decode("utf-8", errors="replace").splitlines()
            if size > 512 * 1024 and lines:
                lines = lines[1:]
            filtered = [
                self._sanitize_log_line(line)
                for line in lines
                if line.strip()
            ][-number:]
        except OSError as exc:
            return f"读取运行日志失败：{exc}"
        if not filtered:
            return "运行日志中没有可显示的内容"
        return f"最近 {len(filtered)} 条运行日志（{path.name}，已脱敏）：\n" + "\n".join(filtered)

    def _cache_files(self) -> list[Path]:
        result: list[Path] = []
        for name in ("inbound-images", "chatgpt-images"):
            directory = (self.config.runtime_dir / name).resolve()
            if not directory.is_dir():
                continue
            result.extend(path for path in directory.iterdir() if path.is_file())
        return result

    def _cache_status(self) -> str:
        files = self._cache_files()
        total_bytes = 0
        readable = 0
        for path in files:
            try:
                total_bytes += path.stat().st_size
                readable += 1
            except OSError:
                pass
        return f"图片缓存：{readable} 个文件，共 {total_bytes / 1024 / 1024:.1f} MB"

    def _clear_cache(self, days: int) -> str:
        if self.chat_runner.active:
            return "ChatGPT 请求正在执行，暂不能清理图片缓存"
        cutoff = time.time() - days * 86400
        protected = set(self.chat_runner.protected_image_paths())
        protected.update(
            Path(item.payload).resolve()
            for item in self._outbox
            if item.kind == "image" and Path(item.payload).is_file()
        )
        removed = 0
        removed_bytes = 0
        for path in self._cache_files():
            try:
                resolved = path.resolve()
                stat = resolved.stat()
                if resolved in protected or stat.st_mtime >= cutoff:
                    continue
                size = stat.st_size
                resolved.unlink()
                removed += 1
                removed_bytes += size
            except OSError:
                log.warning("清理图片缓存失败：%s", path, exc_info=True)
        return (
            f"已清理 {days} 天前的图片缓存：{removed} 个文件，"
            f"释放 {removed_bytes / 1024 / 1024:.1f} MB"
        )

    def _doctor_status(self) -> str:
        wechat = "已连接" if self.wechat.connected else "未连接"
        browser = "运行中" if self.chat_runner.browser_running else "未运行"
        outbox = f"出站队列：待发送 {len(self._outbox)}，死信 {len(self._dead_letters)}"
        inbound_dead_letters = getattr(self.wechat, "inbound_dead_letter_count", 0)
        outbox += f"；入站死信 {inbound_dead_letters}"
        if self._dead_letters:
            latest = self._dead_letters[-1]
            failed_at = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest.failed_at))
                if latest.failed_at
                else "未知时间"
            )
            detail = self._sanitize_log_line(latest.last_error or "未知错误")[:160]
            outbox += f"\n最近死信：{failed_at}，{detail}"
        return (
            "健康检查\n"
            f"微信 iLink：{wechat}\n"
            f"ChatGPT 浏览器：{browser}\n"
            f"ChatGPT 状态：{self.chat_runner.status()}\n"
            f"Codex 状态：{self.runner.status()}\n"
            f"{outbox}\n"
            f"{self._cache_status()}"
        )
