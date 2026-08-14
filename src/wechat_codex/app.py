from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .chatgpt_runner import ChatGPTRunner
from .codex_runner import CodexRunner
from .config import AppConfig
from .ilink_api import ILinkError
from .ilink_client import ILinkClient, IncomingMessage, ReplyTarget
from .router import RouteKind, help_text, route_message, suggest_command


log = logging.getLogger(__name__)

_LOG_SECRET = re.compile(
    r"(?i)(authorization|bearer|token|context_token|aes[_-]?key|"
    r"encrypt_query_param)(\s*[=:]\s*|\s+)([^\s,;]+)"
)
_WECHAT_ID = re.compile(r"[A-Za-z0-9_.-]+@im\.(?:wechat|bot)", re.IGNORECASE)


@dataclass(frozen=True)
class _OutboundItem:
    kind: str
    payload: str
    target: ReplyTarget


@dataclass(frozen=True)
class _PendingChat:
    message_key: str
    session_key: str
    prompt: str
    conversation_title: str
    image_paths: tuple[str, ...]
    target: ReplyTarget


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
        self._project_selection_file = config.runtime_dir / "selected_projects.json"
        self._selected_projects = self._load_selected_projects()
        self._outbox_file = config.runtime_dir / "outbound_queue.json"
        self._outbox = self._load_outbox()
        self._pending_file = config.runtime_dir / "pending_chats.json"
        self._pending_chats = self._load_pending_chats()
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

    def _send_event(
        self,
        text: str,
        session_key: str | None,
        image_paths: tuple[str, ...] = (),
        file_paths: tuple[str, ...] = (),
    ) -> None:
        """Durably enqueue an asynchronous result before attempting delivery."""
        self._queue_event(text, session_key, image_paths, file_paths)
        self._flush_outbox()

    def _queue_event(
        self,
        text: str,
        session_key: str | None,
        image_paths: tuple[str, ...] = (),
        file_paths: tuple[str, ...] = (),
    ) -> None:
        target = self._reply_targets.get(session_key or "")
        if target is None:
            try:
                target = self.wechat.default_target()
            except Exception:
                log.error("异步回复缺少可用的 iLink 目标，已丢弃：%s", text)
                return
        items: list[_OutboundItem] = []
        if text.strip():
            items.append(_OutboundItem("text", text, target))
        for image_path in image_paths:
            items.append(_OutboundItem("image", image_path, target))
        for file_path in file_paths:
            items.append(_OutboundItem("file", file_path, target))
        if not items:
            return
        self._outbox.extend(items)
        self._save_outbox()

    def _load_outbox(self) -> list[_OutboundItem]:
        if not self._outbox_file.is_file():
            return []
        try:
            raw = json.loads(self._outbox_file.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("顶层不是 JSON 数组")
            result: list[_OutboundItem] = []
            for value in raw:
                if not isinstance(value, dict):
                    continue
                kind = str(value.get("kind") or "")
                payload = str(value.get("payload") or "")
                user_id = str(value.get("user_id") or "")
                if kind not in {"text", "image", "file"} or not payload or not user_id:
                    continue
                result.append(
                    _OutboundItem(
                        kind,
                        payload,
                        ReplyTarget(user_id, str(value.get("context_token") or "")),
                    )
                )
            return result
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("忽略无法读取的异步出站队列：%s", exc)
            return []

    def _save_outbox(self) -> None:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._outbox_file.with_suffix(".json.tmp")
        payload = [
            {
                "kind": item.kind,
                "payload": item.payload,
                "user_id": item.target.user_id,
                "context_token": item.target.context_token,
            }
            for item in self._outbox
        ]
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._outbox_file)

    def _flush_outbox(self) -> None:
        while self._outbox:
            item = self._outbox[0]
            if item.kind == "text":
                self._send(item.payload, item.target)
            elif item.kind == "image":
                self.wechat.send_image(item.payload, item.target)
                self._emit_event("outgoing", "微信 Bot", "[图片]")
            else:
                self.wechat.send_file(item.payload, item.target)
                self._emit_event(
                    "outgoing", "微信 Bot", f"[文件] {Path(item.payload).name}"
                )
            self._outbox.pop(0)
            self._save_outbox()

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

                self._flush_outbox()
                runner_events = self.runner.drain_events()
                chat_events = self.chat_runner.drain_events()
                for event in runner_events:
                    self._queue_event(event.text, event.session_key)
                for event in chat_events:
                    self._queue_event(
                        event.text,
                        event.session_key,
                        event.image_paths,
                        event.file_paths,
                    )
                self._flush_outbox()
                self._start_next_pending_chat()

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
        target = message.reply_target
        if message.image_item is not None:
            try:
                message = self.wechat.materialize_image(message)
            except ILinkError as exc:
                log.warning("接收微信图片失败：%s", exc)
                self._send(f"图片接收失败，请重新发送（{exc}）", target)
                return
        log.info("收到 iLink 消息（%s）：%s", message.sender_id, message.content)
        self._emit_event("incoming", message.sender, message.content)
        session_key = self._chat_session_key(message)
        self._reply_targets[session_key] = target
        self._acknowledge(message, target)
        self._handle(message, target, session_key)

    def _acknowledge(self, message: IncomingMessage, target: ReplyTarget) -> None:
        if not self.config.send_received_ack or message.content.lstrip().startswith("@"):
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
            status = self.chat_runner.status() if self.chat_runner.active else self.runner.status()
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
            ok, response = self.chat_runner.begin_archive(session_key)
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.EXPORT_CHAT:
            ok, response = self.chat_runner.begin_export(
                session_key,
                self.config.chatgpt_conversation_title,
            )
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.SUMMARIZE_CHAT:
            ok, response = self.chat_runner.begin_summary(session_key)
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.RENAME_CHAT:
            ok, response = self.chat_runner.begin_rename(session_key, route.prompt)
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.RETRY:
            ok, response = self.chat_runner.begin_retry(session_key)
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
            days = route.days or 7
            if not 1 <= days <= 3650:
                self._send("缓存保留天数必须在 1 到 3650 天之间", target)
                return
            self._send(self._clear_cache(days), target)
            return
        if route.kind == RouteKind.DOCTOR:
            self._send(self._doctor_status(), target)
            return
        if route.kind == RouteKind.VIEW_LOGS:
            number = route.number or 50
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
            ok, response = self.chat_runner.begin_reset(session_key)
            if not ok:
                self._send(response, target)
            return
        if route.kind == RouteKind.CONTINUE:
            if self.chat_runner.active:
                self._send("ChatGPT 请求正在执行，请发送 @状态 或 @停止", target)
                return
            ok, response = self.runner.begin_continue(route.prompt, session_key)
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
            ok, response = self.runner.begin_work(
                project, project_path, route.prompt, session_key
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
            self._enqueue_pending_chat(pending)
            if not self.chat_runner.active:
                self._start_next_pending_chat()
            return
        ok, response = self.chat_runner.begin_chat(
            session_key,
            prompt,
            conversation_title=pending.conversation_title,
            image_paths=pending.image_paths,
        )
        if not ok:
            self._send(response, target)

    def _enqueue_pending_chat(self, pending: _PendingChat) -> None:
        if any(item.message_key == pending.message_key for item in self._pending_chats):
            return
        self._pending_chats.append(pending)
        self._save_pending_chats()
        self._send(
            f"当前请求仍在处理；本消息已排队（第 {len(self._pending_chats)} 位）",
            pending.target,
        )

    def _start_next_pending_chat(self) -> None:
        if not self._pending_chats or self.chat_runner.active or self.runner.active:
            return
        pending = self._pending_chats[0]
        self._reply_targets[pending.session_key] = pending.target
        ok, response = self.chat_runner.begin_chat(
            pending.session_key,
            pending.prompt,
            conversation_title=pending.conversation_title,
            image_paths=pending.image_paths,
        )
        if not ok and self.chat_runner.active:
            return
        self._pending_chats.pop(0)
        self._save_pending_chats()
        if not ok:
            self._send_event(response, pending.session_key)

    def _load_pending_chats(self) -> list[_PendingChat]:
        if not self._pending_file.is_file():
            return []
        try:
            raw = json.loads(self._pending_file.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("顶层不是 JSON 数组")
            result: list[_PendingChat] = []
            for value in raw:
                if not isinstance(value, dict):
                    continue
                message_key = str(value.get("message_key") or "")
                session_key = str(value.get("session_key") or "")
                prompt = str(value.get("prompt") or "")
                user_id = str(value.get("user_id") or "")
                if not message_key or not session_key or not prompt or not user_id:
                    continue
                raw_images = value.get("image_paths")
                image_paths = (
                    tuple(str(path) for path in raw_images if str(path))
                    if isinstance(raw_images, list)
                    else ()
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
            log.warning("忽略无法读取的待处理消息队列：%s", exc)
            return []

    def _save_pending_chats(self) -> None:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._pending_file.with_suffix(".json.tmp")
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
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._pending_file)

    @staticmethod
    def _chat_session_key(message: IncomingMessage) -> str:
        return f"ilink:{message.account_id}:{message.sender_id}"

    def _chat_conversation_title(self, message: IncomingMessage) -> str:
        return self.config.chatgpt_conversation_title

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
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._project_selection_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._selected_projects, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._project_selection_file)

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
        cleaned = _LOG_SECRET.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[已隐藏]",
            line,
        )
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
        return (
            "健康检查\n"
            f"微信 iLink：{wechat}\n"
            f"ChatGPT 浏览器：{browser}\n"
            f"ChatGPT 状态：{self.chat_runner.status()}\n"
            f"Codex 状态：{self.runner.status()}\n"
            f"{self._cache_status()}"
        )
