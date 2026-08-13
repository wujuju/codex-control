from __future__ import annotations

import logging
import re
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .chatgpt_runner import ChatGPTRunner
from .codex_runner import CodexRunner
from .config import AppConfig
from .router import RouteKind, help_text, route_message


log = logging.getLogger(__name__)
Reply = Callable[[str, str], None]


@dataclass(frozen=True)
class WeComMessage:
    key: str
    request_id: str
    sender: str
    chat_id: str
    chat_type: str
    content: str
    msg_type: str

    @property
    def session_key(self) -> str:
        if self.chat_type == "group":
            return f"wecom:group:{self.chat_id}:{self.sender}"
        return f"wecom:single:{self.sender}"

    def conversation_title(self, config: AppConfig) -> str:
        if self.chat_type == "group":
            return (
                config.group_chat_names.get(self.chat_id)
                or config.default_group_chat_name
                or self.chat_id
            )
        return config.user_chat_names.get(self.sender) or self.sender


@dataclass(frozen=True)
class ReplyTarget:
    request_id: str
    reply: Reply


def _strip_group_mention(text: str) -> str:
    """Remove the leading @bot token included in group callbacks."""
    return re.sub(r"^\s*@\S+\s*", "", text, count=1).strip()


def parse_wecom_message(payload: dict[str, Any]) -> WeComMessage | None:
    if payload.get("cmd") != "aibot_msg_callback":
        return None
    headers = payload.get("headers") or {}
    body = payload.get("body") or {}
    if not isinstance(headers, dict) or not isinstance(body, dict):
        raise ValueError("企业微信机器人回调格式无效")

    request_id = str(headers.get("req_id") or "").strip()
    sender_data = body.get("from") or {}
    sender = str(sender_data.get("userid") or "").strip()
    msg_type = str(body.get("msgtype") or "").strip().lower()
    chat_type = str(body.get("chattype") or "").strip().lower()
    chat_id = str(body.get("chatid") or "").strip()
    if not request_id or not sender:
        raise ValueError("企业微信机器人回调缺少 req_id 或 userid")
    if chat_type not in {"single", "group"}:
        raise ValueError(f"未知的企业微信会话类型：{chat_type or '(空)'}")
    if chat_type == "group" and not chat_id:
        raise ValueError("企业微信群消息缺少 chatid")

    content = ""
    if msg_type == "text":
        text_data = body.get("text") or {}
        content = str(text_data.get("content") or "").strip()
        if chat_type == "group":
            content = _strip_group_mention(content)

    key = str(body.get("msgid") or request_id).strip()
    return WeComMessage(
        key=key,
        request_id=request_id,
        sender=sender,
        chat_id=chat_id or sender,
        chat_type=chat_type,
        content=content,
        msg_type=msg_type,
    )


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = "\n\n[回复过长，已截断]"
    budget = max_bytes - len(suffix.encode("utf-8"))
    shortened = encoded[: max(0, budget)].decode("utf-8", errors="ignore")
    return shortened.rstrip() + suffix


class WeComBridge:
    def __init__(
        self,
        config: AppConfig,
        *,
        chat_runner: ChatGPTRunner | None = None,
        codex_runner: CodexRunner | None = None,
    ) -> None:
        self.config = config
        self.chat_runner = chat_runner or ChatGPTRunner(
            browser_channel=config.chatgpt_browser_channel,
            headless=config.chatgpt_headless,
            timeout_seconds=config.chat_timeout_seconds,
            runtime_dir=config.runtime_dir,
        )
        self.runner = codex_runner or CodexRunner(
            config.codex_command,
            config.runtime_dir,
            config.work_timeout_seconds,
        )
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="wecom")
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()
        self._codex_lock = threading.RLock()
        self._codex_owner: ReplyTarget | None = None
        self._stop_event = threading.Event()
        self._dispatcher = threading.Thread(
            target=self._dispatch_codex_events,
            name="codex-wecom-dispatcher",
            daemon=True,
        )
        self._dispatcher.start()

    def submit(self, payload: dict[str, Any], reply: Reply) -> bool:
        message = parse_wecom_message(payload)
        if message is None:
            return False
        log.info(
            "收到企业微信消息：chat_type=%s chatid=%s userid=%s msgid=%s",
            message.chat_type,
            message.chat_id,
            message.sender,
            message.key,
        )
        if self.config.group_only and message.chat_type != "group":
            log.info("忽略单聊消息；当前配置 group_only=true")
            return False
        allowed_groups = self.config.allowed_group_chat_ids
        if (
            message.chat_type == "group"
            and allowed_groups
            and message.chat_id not in allowed_groups
        ):
            log.warning("忽略未授权企业微信群：%s", message.chat_id)
            return False
        with self._seen_lock:
            if message.key in self._seen:
                return False
            self._seen.add(message.key)
            self._seen_order.append(message.key)
            while len(self._seen_order) > 10000:
                self._seen.discard(self._seen_order.popleft())
        self._executor.submit(self._handle_safely, message, reply)
        return True

    def close(self) -> None:
        self._stop_event.set()
        self.chat_runner.stop_all()
        self.runner.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _handle_safely(self, message: WeComMessage, reply: Reply) -> None:
        try:
            self._handle(message, reply)
        except Exception as exc:
            log.exception("处理企业微信机器人消息失败：%s", message.sender)
            try:
                self._send(
                    ReplyTarget(message.request_id, reply),
                    f"消息处理失败：{type(exc).__name__}: {exc}",
                )
            except Exception:
                log.exception("发送企业微信错误通知失败")

    def _handle(self, message: WeComMessage, reply: Reply) -> None:
        target = ReplyTarget(message.request_id, reply)
        if message.msg_type != "text" or not message.content:
            self._send(target, "当前群测试仅支持文字消息")
            return
        route = route_message(message.content)
        session_key = message.session_key
        if (
            route.kind
            in {
                RouteKind.WORK,
                RouteKind.CONTINUE,
                RouteKind.STATUS,
                RouteKind.STOP,
            }
            and message.sender not in self.config.authorized_senders
        ):
            log.warning("拒绝未授权的 Codex 操作请求，发送者：%s", message.sender)
            self._send(target, "无权限执行 Codex 操作")
            return

        if route.kind == RouteKind.HELP:
            self._send(
                target,
                help_text(list(self.config.projects), self.config.default_project),
            )
            return
        if route.kind == RouteKind.STATUS:
            statuses = [self.chat_runner.status(session_key), self.runner.status()]
            self._send(target, "\n".join(statuses))
            return
        if route.kind == RouteKind.STOP:
            chat_status = self.chat_runner.stop(session_key)
            codex_status = self.runner.stop()
            self._send(target, f"{chat_status}\n{codex_status}")
            return
        if route.kind == RouteKind.NEW_CHAT:
            _ok, response = self.chat_runner.reset(session_key)
            self._send(target, response)
            return
        if route.kind == RouteKind.CONTINUE:
            if self.chat_runner.is_active(session_key):
                self._send(target, "当前对话的 ChatGPT Plus 请求仍在执行")
                return
            with self._codex_lock:
                previous_owner = self._codex_owner
                self._codex_owner = target
                ok, response = self.runner.begin_continue(route.prompt)
                if not ok:
                    self._codex_owner = previous_owner
            self._send(target, response)
            return
        if route.kind == RouteKind.WORK:
            if self.chat_runner.is_active(session_key):
                self._send(target, "当前对话的 ChatGPT Plus 请求仍在执行")
                return
            project = route.project or self.config.default_project
            project_path = self.config.projects.get(project)
            if project_path is None:
                self._send(
                    target,
                    f"未知项目 {project!r}；可用项目：{'、'.join(self.config.projects)}",
                )
                return
            with self._codex_lock:
                previous_owner = self._codex_owner
                self._codex_owner = target
                ok, response = self.runner.begin_work(
                    project, project_path, route.prompt
                )
                if not ok:
                    self._codex_owner = previous_owner
            self._send(target, response)
            return

        ok, response = self.chat_runner.begin_chat(
            session_key,
            route.prompt,
            lambda text: self._send(target, text),
            conversation_title=message.conversation_title(self.config),
        )
        if not ok:
            self._send(target, response)

    def _send(self, target: ReplyTarget, text: str) -> None:
        payload = _truncate_utf8(
            self.config.response_prefix + text,
            self.config.max_reply_bytes,
        )
        target.reply(target.request_id, payload)

    def _dispatch_codex_events(self) -> None:
        while not self._stop_event.is_set():
            events = self.runner.drain_events()
            with self._codex_lock:
                owner = self._codex_owner
            if owner:
                for event in events:
                    try:
                        self._send(owner, event.text)
                    except Exception:
                        log.exception("发送 Codex 企业微信通知失败")
            elif events:
                log.error("Codex 返回了结果，但没有可用的企业微信接收目标")
            self._stop_event.wait(0.2)
