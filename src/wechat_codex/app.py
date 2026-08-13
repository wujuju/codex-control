from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

from .chatgpt_runner import ChatGPTRunner
from .codex_runner import CodexRunner
from .config import AppConfig
from .router import RouteKind, help_text, route_message
from .wechat_client import IncomingMessage, WeChatClient


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
        self.wechat = WeChatClient(
            contact=config.contact,
            background_mode=config.background_mode,
            allow_self_messages=config.allow_self_messages,
            voice_recognition=config.voice_recognition,
            voice_retry_count=config.voice_retry_count,
            response_prefix=config.response_prefix,
            max_reply_chars=config.max_reply_chars,
            chat_type=config.chat_type,
            bot_name=config.bot_name,
        )
        self.runner = CodexRunner(
            codex_command=config.codex_command,
            runtime_dir=config.runtime_dir,
            work_timeout_seconds=config.work_timeout_seconds,
        )
        self.chat_runner = ChatGPTRunner(
            model=config.chat_model,
            reasoning_effort=config.chat_reasoning_effort,
            max_output_tokens=config.chat_max_output_tokens,
            timeout_seconds=config.chat_timeout_seconds,
            runtime_dir=config.runtime_dir,
        )
        self._announced = False

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    self._emit_state("connecting", "正在连接微信")
                    self.wechat.connect()
                    baseline_count = self.wechat.baseline()
                    log.info(
                        "已连接微信联系人 %s，记录 %d 条已有消息；等待新消息",
                        self.config.contact,
                        baseline_count,
                    )
                    self._emit_state("running", f"正在监听 {self.config.contact}")
                    if self.config.send_ready_message and not self._announced:
                        voice = "，支持自动识别语音" if self.config.voice_recognition else ""
                        self._send(f"已上线{voice}。发送“帮助”查看命令。")
                        self._announced = True
                    self._listen()
                except KeyboardInterrupt:
                    log.info("收到退出信号")
                    self.stop()
                except Exception as exc:
                    log.warning("微信尚未就绪，5 秒后重试：%s", exc)
                    self._emit_state("reconnecting", f"连接失败，正在重试：{exc}")
                    self._stop_event.wait(5)
        finally:
            self.chat_runner.stop()
            self.runner.stop()
            self._emit_state("stopped", "已停止")

    def stop(self) -> None:
        self._stop_event.set()
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

    def _send(self, text: str) -> None:
        self.wechat.send(text)
        self._emit_event("outgoing", self.config.bot_name, text)

    def _listen(self) -> None:
        consecutive_errors = 0
        while not self._stop_event.is_set():
            try:
                while True:
                    try:
                        self._send(self._manual_messages.get_nowait())
                    except queue.Empty:
                        break

                for event in self.runner.drain_events():
                    self._send(event.text)
                for event in self.chat_runner.drain_events():
                    self._send(event.text)

                for message in self.wechat.poll():
                    log.info(
                        "收到消息（%s/%s/%s）：%s",
                        message.chat_type,
                        message.attr,
                        message.sender,
                        message.content,
                    )
                    self._emit_event("incoming", message.sender, message.content)
                    self._handle(message)
                consecutive_errors = 0
            except KeyboardInterrupt:
                raise
            except Exception:
                consecutive_errors += 1
                log.exception("微信轮询或发送失败（%d/5）", consecutive_errors)
                if consecutive_errors >= 5:
                    raise RuntimeError("微信连续 5 次访问失败，准备重新连接")
            self._stop_event.wait(self.config.poll_seconds)

    def _handle(self, message: IncomingMessage) -> None:
        route = route_message(message.content)
        if route.kind in {
            RouteKind.WORK,
            RouteKind.CONTINUE,
            RouteKind.STATUS,
            RouteKind.STOP,
        } and message.sender not in self.config.authorized_senders:
            log.warning("拒绝未授权的 Codex 操作请求，发送者：%s", message.sender)
            self._send("无权限执行 Codex 操作")
            return
        if route.kind == RouteKind.HELP:
            self._send(
                help_text(list(self.config.projects), self.config.default_project)
            )
            return
        if route.kind == RouteKind.STATUS:
            status = self.chat_runner.status() if self.chat_runner.active else self.runner.status()
            self._send(status)
            return
        if route.kind == RouteKind.STOP:
            result = self.chat_runner.stop() if self.chat_runner.active else self.runner.stop()
            self._send(result)
            return
        session_key = self._chat_session_key(message)
        if route.kind == RouteKind.NEW_CHAT:
            if self.runner.active:
                self._send("Codex 任务正在执行，请发送“状态”或“停止”")
                return
            ok, response = self.chat_runner.begin_reset(session_key)
            if not ok:
                self._send(response)
            return
        if route.kind == RouteKind.CONTINUE:
            if self.chat_runner.active:
                self._send("ChatGPT 请求正在执行，请发送“状态”或“停止”")
                return
            ok, response = self.runner.begin_continue(route.prompt)
            if not ok:
                self._send(response)
            return
        if route.kind == RouteKind.WORK:
            if self.chat_runner.active:
                self._send("ChatGPT 请求正在执行，请发送“状态”或“停止”")
                return
            project = route.project or self.config.default_project
            project_path = self.config.projects.get(project)
            if project_path is None:
                self._send(
                    f"未知项目 {project!r}；可用项目：{'、'.join(self.config.projects)}"
                )
                return
            ok, response = self.runner.begin_work(project, project_path, route.prompt)
            if not ok:
                self._send(response)
            return

        if self.runner.active:
            self._send("Codex 任务正在执行，请发送“状态”或“停止”")
            return
        ok, response = self.chat_runner.begin_chat(session_key, route.prompt)
        if not ok:
            self._send(response)

    def _chat_session_key(self, message: IncomingMessage) -> str:
        conversation = message.conversation or self.config.contact
        if message.chat_type == "group":
            return f"group:{conversation}:{message.sender}"
        return f"friend:{conversation}"
