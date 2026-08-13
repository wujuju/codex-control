from __future__ import annotations

import logging
import time

from .codex_runner import CodexRunner
from .config import AppConfig
from .router import RouteKind, help_text, route_message
from .wechat_client import IncomingMessage, WeChatClient


log = logging.getLogger(__name__)


class BridgeApp:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
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
            chat_timeout_seconds=config.chat_timeout_seconds,
            work_timeout_seconds=config.work_timeout_seconds,
        )
        self._announced = False

    def run(self) -> None:
        while True:
            try:
                self.wechat.connect()
                baseline_count = self.wechat.baseline()
                log.info(
                    "已连接微信联系人 %s，记录 %d 条已有消息；等待新消息",
                    self.config.contact,
                    baseline_count,
                )
                if self.config.send_ready_message and not self._announced:
                    voice = "，支持自动识别语音" if self.config.voice_recognition else ""
                    self.wechat.send(f"已上线{voice}。发送“帮助”查看命令。")
                    self._announced = True
                self._listen()
            except KeyboardInterrupt:
                log.info("收到退出信号")
                self.runner.stop()
                return
            except Exception as exc:
                log.warning("微信尚未就绪，5 秒后重试：%s", exc)
                time.sleep(5)

    def _listen(self) -> None:
        consecutive_errors = 0
        while True:
            try:
                for event in self.runner.drain_events():
                    self.wechat.send(event.text)

                for message in self.wechat.poll():
                    log.info(
                        "收到消息（%s/%s/%s）：%s",
                        message.chat_type,
                        message.attr,
                        message.sender,
                        message.content,
                    )
                    self._handle(message)
                consecutive_errors = 0
            except KeyboardInterrupt:
                raise
            except Exception:
                consecutive_errors += 1
                log.exception("微信轮询或发送失败（%d/5）", consecutive_errors)
                if consecutive_errors >= 5:
                    raise RuntimeError("微信连续 5 次访问失败，准备重新连接")
            time.sleep(self.config.poll_seconds)

    def _handle(self, message: IncomingMessage) -> None:
        route = route_message(message.content)
        if route.kind in {
            RouteKind.WORK,
            RouteKind.CONTINUE,
            RouteKind.STATUS,
            RouteKind.STOP,
        } and message.sender not in self.config.authorized_senders:
            log.warning("拒绝未授权的 Codex 操作请求，发送者：%s", message.sender)
            self.wechat.send("无权限执行 Codex 操作")
            return
        if route.kind == RouteKind.HELP:
            self.wechat.send(
                help_text(list(self.config.projects), self.config.default_project)
            )
            return
        if route.kind == RouteKind.STATUS:
            self.wechat.send(self.runner.status())
            return
        if route.kind == RouteKind.STOP:
            self.wechat.send(self.runner.stop())
            return
        if route.kind == RouteKind.CONTINUE:
            ok, response = self.runner.begin_continue(route.prompt)
            if not ok:
                self.wechat.send(response)
            return
        if route.kind == RouteKind.WORK:
            project = route.project or self.config.default_project
            project_path = self.config.projects.get(project)
            if project_path is None:
                self.wechat.send(
                    f"未知项目 {project!r}；可用项目：{'、'.join(self.config.projects)}"
                )
                return
            ok, response = self.runner.begin_work(project, project_path, route.prompt)
            if not ok:
                self.wechat.send(response)
            return

        ok, response = self.runner.begin_chat(route.prompt)
        if not ok:
            self.wechat.send(response)
