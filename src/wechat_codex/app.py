from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

from .chatgpt_runner import ChatGPTRunner
from .codex_runner import CodexRunner
from .config import AppConfig
from .ilink_api import ILinkError
from .ilink_client import ILinkClient, IncomingMessage, ReplyTarget
from .router import RouteKind, help_text, route_message


log = logging.getLogger(__name__)


class BridgeApp:
    def __init__(
        self,
        config: AppConfig,
        *,
        event_sink: Callable[[str, str, str], None] | None = None,
        state_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self._event_sink = event_sink
        self._state_sink = state_sink
        self._stop_event = threading.Event()
        self._manual_messages: queue.Queue[str] = queue.Queue()
        self._reply_targets: dict[str, ReplyTarget] = {}
        self.wechat = ILinkClient(
            credentials_path=config.ilink_credentials_file,
            state_path=config.ilink_state_file,
            response_prefix=config.response_prefix,
            max_reply_chars=config.max_reply_chars,
            long_poll_timeout_seconds=config.ilink_long_poll_timeout_seconds,
        )
        self.runner = CodexRunner(
            codex_command=config.codex_command,
            runtime_dir=config.runtime_dir,
            work_timeout_seconds=config.work_timeout_seconds,
        )
        self.chat_runner = ChatGPTRunner(
            browser_channel=config.chatgpt_browser_channel,
            headless=config.chatgpt_headless,
            proxy_server=config.chatgpt_proxy_server,
            timeout_seconds=config.chat_timeout_seconds,
            runtime_dir=config.runtime_dir,
        )

    def run(self) -> None:
        try:
            self._emit_state("connecting", "正在启动 ChatGPT 专用浏览器")
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
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    log.warning("微信 iLink 连接中断，5 秒后重试：%s", exc)
                    self._emit_state("reconnecting", f"iLink 连接失败，正在重试：{exc}")
                    self.wechat.close()
                    self._stop_event.wait(5)
        finally:
            self.wechat.close()
            self.chat_runner.close()
            self.runner.stop()
            self._emit_state("stopped", "已停止")

    def stop(self) -> None:
        self._stop_event.set()
        self.wechat.close()
        self.chat_runner.stop()
        self.runner.stop()

    def enqueue_message(self, text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            self._manual_messages.put(cleaned)

    def _emit_event(self, kind: str, sender: str, text: str) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(kind, sender, text)
        except Exception:
            log.exception("界面消息回调失败")

    def _emit_state(self, state: str, text: str) -> None:
        if self._state_sink is None:
            return
        try:
            self._state_sink(state, text)
        except Exception:
            log.exception("界面状态回调失败")

    def _send(self, text: str, target: ReplyTarget) -> None:
        self.wechat.send(text, target)
        self._emit_event("outgoing", "微信 Bot", text)

    def _send_event(self, text: str, session_key: str | None) -> None:
        target = self._reply_targets.get(session_key or "")
        if target is None:
            try:
                target = self.wechat.default_target()
            except Exception:
                log.error("异步回复缺少可用的 iLink 目标，已丢弃：%s", text)
                return
        self._send(text, target)

    def _listen(self) -> None:
        consecutive_errors = 0
        while not self._stop_event.is_set():
            try:
                while True:
                    try:
                        manual = self._manual_messages.get_nowait()
                    except queue.Empty:
                        break
                    self._send(manual, self.wechat.default_target())

                for event in self.runner.drain_events():
                    self._send_event(event.text, event.session_key)
                for event in self.chat_runner.drain_events():
                    self._send_event(event.text, event.session_key)

                for message in self.wechat.poll():
                    try:
                        self._process_incoming(message)
                    except Exception:
                        self.wechat.retry(message)
                        raise
                    else:
                        self.wechat.acknowledge(message.key)
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
        if not self._is_allowed(message.sender_id):
            log.warning("忽略未授权的 iLink 消息发送者：%s", message.sender_id)
            return
        log.info("收到 iLink 消息（%s）：%s", message.sender_id, message.content)
        self._emit_event("incoming", message.sender, message.content)
        session_key = self._chat_session_key(message)
        target = message.reply_target
        self._reply_targets[session_key] = target
        self._acknowledge(message, target)
        self._handle(message, target, session_key)

    def _acknowledge(self, message: IncomingMessage, target: ReplyTarget) -> None:
        if not self.config.send_received_ack:
            return
        try:
            self._send(self.config.received_ack_text, target)
        except Exception:
            log.exception("发送收到确认失败，继续处理原消息")

    def _handle(
        self,
        message: IncomingMessage,
        target: ReplyTarget,
        session_key: str,
    ) -> None:
        route = route_message(message.content)
        codex_commands = {
            RouteKind.WORK, RouteKind.CONTINUE, RouteKind.STATUS, RouteKind.STOP
        }
        if route.kind in codex_commands and not self._can_run_codex(message.sender_id):
            log.warning("拒绝未授权的 Codex 请求：%s", message.sender_id)
            self._send("无权限执行 Codex 操作", target)
            return
        if route.kind == RouteKind.HELP:
            self._send(help_text(list(self.config.projects), self.config.default_project), target)
            return
        if route.kind == RouteKind.STATUS:
            status = self.chat_runner.status() if self.chat_runner.active else self.runner.status()
            self._send(status, target)
            return
        if route.kind == RouteKind.STOP:
            result = self.chat_runner.stop() if self.chat_runner.active else self.runner.stop()
            self._send(result, target)
            return
        if route.kind == RouteKind.NEW_CHAT:
            if self.runner.active:
                self._send("Codex 任务正在执行，请发送“状态”或“停止”", target)
                return
            ok, response = self.chat_runner.begin_reset(session_key)
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.CONTINUE:
            if self.chat_runner.active:
                self._send("ChatGPT 请求正在执行，请发送“状态”或“停止”", target)
                return
            ok, response = self.runner.begin_continue(route.prompt, session_key)
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.WORK:
            if self.chat_runner.active:
                self._send("ChatGPT 请求正在执行，请发送“状态”或“停止”", target)
                return
            project = route.project or self.config.default_project
            project_path = self.config.projects.get(project)
            if project_path is None:
                self._send(
                    f"未知项目 {project!r}；可用项目：{'、'.join(self.config.projects)}",
                    target,
                )
                return
            ok, response = self.runner.begin_work(
                project, project_path, route.prompt, session_key
            )
            if not ok:
                self._send(response, target)
            return

        if self.runner.active:
            self._send("Codex 任务正在执行，请发送“状态”或“停止”", target)
            return
        ok, response = self.chat_runner.begin_chat(
            session_key,
            route.prompt,
            conversation_title=self._chat_conversation_title(message),
        )
        if not ok:
            self._send(response, target)

    @staticmethod
    def _chat_session_key(message: IncomingMessage) -> str:
        return f"ilink:{message.account_id}:{message.sender_id}"

    def _chat_conversation_title(self, message: IncomingMessage) -> str:
        return self.config.chatgpt_conversation_title
