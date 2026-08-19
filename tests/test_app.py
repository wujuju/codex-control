import json
import os
import queue
from collections import deque
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from wechat_codex.app import BridgeApp
from wechat_codex.chatgpt_runner import ChatEvent
from wechat_codex.ilink_client import ILinkStateError, IncomingMessage, ReplyTarget


class FakeWeChat:
    def __init__(self) -> None:
        self.user_id = "owner-id"
        self.connected = True
        self.sent: list[tuple[str, ReplyTarget]] = []
        self.sent_images: list[tuple[str, ReplyTarget]] = []
        self.sent_files: list[tuple[str, ReplyTarget]] = []
        self.reconnect_calls = 0
        self.inbound_dead_letter_count = 0
        self.send_failures = 0
        self.ack_failures = 0
        self.fail_image_paths: set[str] = set()
        self.client_ids: list[str | None] = []
        self.acknowledged: list[str] = []
        self.retried: list[IncomingMessage] = []

    def send(
        self,
        text: str,
        target: ReplyTarget,
        *,
        client_id: str | None = None,
    ) -> None:
        if self.send_failures:
            self.send_failures -= 1
            raise OSError("temporary send failure")
        self.client_ids.append(client_id)
        self.sent.append((text, target))

    def send_image(
        self,
        image_path: str,
        target: ReplyTarget,
        *,
        client_id: str | None = None,
    ) -> None:
        if image_path in self.fail_image_paths:
            raise FileNotFoundError(f"文件不存在：{image_path}")
        self.client_ids.append(client_id)
        self.sent_images.append((image_path, target))

    def send_file(
        self,
        file_path: str,
        target: ReplyTarget,
        *,
        client_id: str | None = None,
    ) -> None:
        self.client_ids.append(client_id)
        self.sent_files.append((file_path, target))

    def reconnect(self) -> None:
        self.reconnect_calls += 1

    def default_target(self) -> ReplyTarget:
        return ReplyTarget(self.user_id, "ctx-owner")

    def acknowledge(self, key: str) -> None:
        if self.ack_failures:
            self.ack_failures -= 1
            raise OSError("temporary state failure")
        self.acknowledged.append(key)

    def retry(self, incoming: IncomingMessage) -> None:
        self.retried.append(incoming)


class FakeRunner:
    def __init__(self) -> None:
        self.work_calls: list[tuple] = []
        self.active = False
        self.events = []

    def begin_work(self, *args):
        self.work_calls.append(args)
        return True, "已开始"

    def status(self, session_key=None) -> str:
        return "Codex 当前空闲"

    def recent_tasks(self, session_key=None) -> str:
        return "最近 Codex 任务：\n1. [成功] control：测试"

    def drain_events(self):
        events, self.events = self.events, []
        return events

    def close(self) -> None:
        return None


class FakeChatRunner:
    def __init__(self) -> None:
        self.active = False
        self.browser_running = True
        self.chat_calls: list[tuple[str, str, str | None, tuple[str, ...]]] = []
        self.archive_calls: list[str] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.retry_calls: list[str] = []
        self.export_calls: list[tuple[str, str]] = []
        self.summary_calls: list[str] = []
        self.switch_calls: list[tuple[str, int, str]] = []
        self.restart_calls = 0
        self.last_event: ChatEvent | None = None
        self.protected_paths: frozenset[Path] = frozenset()
        self.events = []

    def begin_chat(
        self, session_key, prompt, conversation_title=None, image_paths=()
    ):
        self.chat_calls.append((session_key, prompt, conversation_title, image_paths))
        return True, "已开始"

    def conversation_info(self, session_key, default_title):
        return f"当前 ChatGPT 对话\n标题：{default_title}\n地址：https://chatgpt.com/c/1"

    def begin_archive(self, session_key):
        self.archive_calls.append(session_key)
        return True, "已开始归档对话"

    def begin_rename(self, session_key, title):
        self.rename_calls.append((session_key, title))
        return True, "已开始重命名对话"

    def begin_retry(self, session_key):
        self.retry_calls.append(session_key)
        return True, "已开始重试"

    def conversation_list(self, session_key, default_title):
        return "最近保存的 ChatGPT 对话：\n1. 测试 [当前]"

    def switch_conversation(self, session_key, number, default_title):
        self.switch_calls.append((session_key, number, default_title))
        return True, "已切换到对话：测试"

    def begin_export(self, session_key, default_title):
        self.export_calls.append((session_key, default_title))
        return True, "已开始导出对话"

    def begin_summary(self, session_key):
        self.summary_calls.append(session_key)
        return True, "已开始总结对话"

    def last_reply(self, session_key):
        if self.last_event is None or self.last_event.session_key != session_key:
            return None
        return self.last_event

    def protected_image_paths(self):
        return self.protected_paths

    def status(self) -> str:
        return "ChatGPT Plus 网页当前空闲"

    def restart(self):
        self.restart_calls += 1
        return True, "ChatGPT 隐藏浏览器已重启"

    def drain_events(self):
        events, self.events = self.events, []
        return events


def message(sender: str = "owner-id", content: str = "你好") -> IncomingMessage:
    return IncomingMessage(
        key=f"ilink:{sender}:{content}",
        content=content,
        sender=sender,
        sender_id=sender,
        reply_to=sender,
        context_token="ctx-1",
        account_id="bot-1",
    )


class BridgeAppTests(unittest.TestCase):
    def make_app(self) -> BridgeApp:
        runtime = TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        app = BridgeApp.__new__(BridgeApp)
        app._event_sink = None
        app._state_sink = None
        app._reply_targets = {}
        app._manual_messages = queue.Queue()
        app._ack_backlog = deque()
        app._current_message_key = None
        app._current_send_index = 0
        app.config = SimpleNamespace(
            send_received_ack=True,
            received_ack_text="已收到",
            ilink_allowed_user_ids=frozenset(),
            ilink_codex_user_ids=frozenset(),
            projects={"control": "."},
            default_project="control",
            chatgpt_conversation_title="微信助手",
            runtime_dir=Path(runtime.name),
        )
        app._stop_request_file = app.config.runtime_dir / "stop.request"
        app._process_started_at = 0.0
        app.wechat = FakeWeChat()
        app.runner = FakeRunner()
        app.chat_runner = FakeChatRunner()
        app._project_selection_file = app.config.runtime_dir / "selected_projects.json"
        app._selected_projects = {}
        app._outbox_file = app.config.runtime_dir / "outbound_queue.json"
        app._outbox = []
        app._dead_letter_file = app.config.runtime_dir / "outbound_dead_letters.json"
        app._dead_letters = []
        app._active_jobs_file = app.config.runtime_dir / "active_jobs.json"
        app._active_jobs = {}
        app._completion_backlog = deque()
        app._pending_file = app.config.runtime_dir / "pending_chats.json"
        app._pending_chats = []
        app._runtime_account_file = app.config.runtime_dir / "runtime_account.json"
        return app

    def test_incoming_ack_uses_same_reply_context(self) -> None:
        app = self.make_app()
        incoming = message()
        app._handle = lambda *_args: None

        app._process_incoming(incoming)

        self.assertEqual(app.wechat.sent, [("已收到", ReplyTarget("owner-id", "ctx-1"))])
        self.assertEqual(
            app._reply_targets["ilink:bot-1:owner-id"], ReplyTarget("owner-id", "ctx-1")
        )

    def test_incoming_batch_retries_current_and_unprocessed_tail(self) -> None:
        app = self.make_app()
        messages = [message("user-a", "第一条"), message("user-a", "第二条")]
        app._process_incoming = lambda _message: (_ for _ in ()).throw(
            OSError("processing failed")
        )

        with self.assertRaises(OSError):
            app._process_incoming_batch(messages)

        self.assertEqual(app.wechat.retried, messages)
        self.assertEqual(list(app._ack_backlog), [])

    def test_replayed_sync_reply_reuses_the_same_client_id(self) -> None:
        app = self.make_app()
        incoming = message("owner-id", "@状态")

        app._process_incoming(incoming)
        app._process_incoming(incoming)

        self.assertEqual(len(app.wechat.client_ids), 2)
        self.assertEqual(app.wechat.client_ids[0], app.wechat.client_ids[1])

    def test_manual_gui_message_moves_to_durable_outbox_before_send(self) -> None:
        app = self.make_app()
        app.enqueue_message("GUI 手工消息")

        app._persist_manual_messages()

        self.assertTrue(app._manual_messages.empty())
        self.assertEqual([item.payload for item in app._outbox], ["GUI 手工消息"])
        app.wechat.send_failures = 1
        app._flush_outbox()
        self.assertEqual([item.payload for item in app._outbox], ["GUI 手工消息"])

    def test_resend_replay_reuses_stable_outbox_client_id(self) -> None:
        app = self.make_app()
        incoming = message("owner-id", "@重发")
        session_key = app._chat_session_key(incoming)
        app.chat_runner.last_event = ChatEvent("上一次回复", session_key)
        app.wechat.ack_failures = 1

        with self.assertRaises(OSError):
            app._process_incoming_batch([incoming])
        app._process_incoming(incoming)

        self.assertEqual(len(app.wechat.client_ids), 2)
        self.assertEqual(app.wechat.client_ids[0], app.wechat.client_ids[1])

    def test_stop_request_only_applies_to_the_target_process(self) -> None:
        app = self.make_app()
        app._stop_request_file.write_text("999999999", encoding="ascii")
        self.assertFalse(app._consume_stop_request())
        self.assertFalse(app._stop_request_file.exists())

        app._stop_request_file.write_text(str(os.getpid()), encoding="ascii")
        self.assertTrue(app._consume_stop_request())
        self.assertFalse(app._stop_request_file.exists())

    def test_incoming_batch_retries_tail_when_ack_checkpoint_fails(self) -> None:
        app = self.make_app()
        messages = [message("user-a", "第一条"), message("user-a", "第二条")]
        app._process_incoming = lambda _message: None
        app.wechat.ack_failures = 1

        with self.assertRaises(OSError):
            app._process_incoming_batch(messages)

        self.assertEqual(app.wechat.retried, messages[1:])
        self.assertEqual(list(app._ack_backlog), [messages[0].key])
        app._flush_ack_backlog()
        self.assertEqual(list(app._ack_backlog), [])
        self.assertEqual(app.wechat.acknowledged, [messages[0].key])

    def test_at_commands_skip_ack_and_unknown_command_skips_chatgpt(self) -> None:
        app = self.make_app()

        app._process_incoming(message(content="@归档"))

        self.assertEqual(len(app.wechat.sent), 1)
        self.assertIn("命令不存在：@归档", app.wechat.sent[0][0])
        self.assertIn("你是否想使用：@归档对话", app.wechat.sent[0][0])
        self.assertNotIn("已收到", app.wechat.sent[0][0])
        self.assertEqual(app.chat_runner.chat_calls, [])

    def test_known_at_command_returns_result_without_ack(self) -> None:
        app = self.make_app()

        app._process_incoming(message(content="@状态"))

        self.assertEqual(
            app.wechat.sent,
            [("Codex 当前空闲", ReplyTarget("owner-id", "ctx-1"))],
        )

    def test_non_owner_is_ignored_when_allowlist_is_empty(self) -> None:
        app = self.make_app()

        app._process_incoming(message("other-id", "干活：运行测试"))

        self.assertEqual(app.wechat.sent, [])
        self.assertEqual(app.runner.work_calls, [])

    def test_codex_allowlist_uses_stable_sender_id_and_keeps_session(self) -> None:
        app = self.make_app()
        app.config = SimpleNamespace(**{
            **app.config.__dict__,
            "ilink_allowed_user_ids": frozenset({"user-a"}),
            "ilink_codex_user_ids": frozenset({"user-a"}),
        })
        incoming = message("user-a", "@干活：运行测试")
        target = incoming.reply_target
        session_key = app._chat_session_key(incoming)

        app._handle(incoming, target, session_key)

        self.assertEqual(len(app.runner.work_calls), 1)
        self.assertEqual(app.runner.work_calls[0][-1], session_key)

    def test_chat_session_isolated_by_bot_and_sender_id(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "你好")

        app._handle(incoming, incoming.reply_target, app._chat_session_key(incoming))

        self.assertEqual(
            app.chat_runner.chat_calls,
            [("ilink:bot-1:user-a", "你好", "微信助手", ())],
        )

    def test_image_is_forwarded_to_chatgpt(self) -> None:
        app = self.make_app()
        incoming = IncomingMessage(
            **{
                **message("user-a", "[图片]").__dict__,
                "image_path": "D:/runtime/inbound-images/photo.jpg",
            }
        )

        app._handle(incoming, incoming.reply_target, app._chat_session_key(incoming))

        self.assertEqual(
            app.chat_runner.chat_calls,
            [(
                "ilink:bot-1:user-a",
                "请分析这张图片，并说明你看到了什么。",
                "微信助手",
                ("D:/runtime/inbound-images/photo.jpg",),
            )],
        )

    def test_message_received_during_chat_is_queued_and_started_later(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "后续问题")
        target = incoming.reply_target
        session_key = app._chat_session_key(incoming)
        app.chat_runner.active = True

        app._handle(incoming, target, session_key)

        self.assertEqual(app.chat_runner.chat_calls, [])
        self.assertEqual(len(app._pending_chats), 1)
        self.assertTrue(app._pending_file.is_file())
        self.assertIn("已排队", app.wechat.sent[-1][0])

        app.chat_runner.active = False
        app._start_next_pending_chat()

        self.assertEqual(
            app.chat_runner.chat_calls,
            [(session_key, "后续问题", "微信助手", ())],
        )
        self.assertEqual(app._pending_chats, [])

    def test_cross_account_chat_start_race_keeps_message_queued(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "不能因并发丢失")
        attempts: list[str] = []

        def lose_shared_runner_race(session_key, *_args, **_kwargs):
            attempts.append(session_key)
            return False, "已有 ChatGPT 请求在执行，请发送 @状态 或 @停止"

        app.chat_runner.begin_chat = lose_shared_runner_race

        app._handle(
            incoming,
            incoming.reply_target,
            app._chat_session_key(incoming),
        )

        self.assertEqual(len(app._pending_chats), 1)
        self.assertEqual(app._pending_chats[0].message_key, incoming.key)
        self.assertEqual(len(attempts), 2)
        self.assertIn("已排队", app.wechat.sent[-1][0])

    def test_pending_claim_checkpoint_failure_restores_in_memory_queue(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "可靠排队")
        session_key = app._chat_session_key(incoming)
        app.chat_runner.active = True
        app._handle(incoming, incoming.reply_target, session_key)
        app.chat_runner.active = False
        save = app._save_pending_chats
        app._save_pending_chats = lambda: (_ for _ in ()).throw(OSError("disk"))

        with self.assertRaises(OSError):
            app._start_next_pending_chat()

        self.assertEqual([item.message_key for item in app._pending_chats], [incoming.key])
        self.assertIn(session_key, app._active_jobs)
        app._save_pending_chats = save
        app._queue_async_result("排队任务结果", session_key)
        self.assertEqual(app._pending_chats, [])

    def test_pending_chat_queue_is_restored_after_restart(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "不能丢失的问题")
        app.chat_runner.active = True
        app._handle(incoming, incoming.reply_target, app._chat_session_key(incoming))

        app._pending_chats = app._load_pending_chats()

        self.assertEqual(len(app._pending_chats), 1)
        self.assertEqual(app._pending_chats[0].prompt, "不能丢失的问题")
        self.assertEqual(app._pending_chats[0].target, incoming.reply_target)

    def test_existing_pending_backlog_is_not_truncated_on_load(self) -> None:
        app = self.make_app()
        payload = [
            {
                "message_key": f"message-{index}",
                "session_key": f"session-{index}",
                "prompt": f"prompt-{index}",
                "conversation_title": "微信助手",
                "image_paths": [],
                "user_id": f"user-{index}",
                "context_token": f"ctx-{index}",
            }
            for index in range(105)
        ]
        app._pending_file.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        restored = app._load_pending_chats()

        self.assertEqual(len(restored), 105)

    def test_pending_chat_deduplicates_retried_ilink_message(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "同一条问题")
        app.chat_runner.active = True

        app._handle(incoming, incoming.reply_target, app._chat_session_key(incoming))
        app._handle(incoming, incoming.reply_target, app._chat_session_key(incoming))

        self.assertEqual(len(app._pending_chats), 1)

    def test_pending_chat_queue_applies_a_hard_limit(self) -> None:
        app = self.make_app()
        app.chat_runner.active = True
        for index in range(101):
            incoming = message("user-a", f"问题 {index}")
            app._handle(incoming, incoming.reply_target, app._chat_session_key(incoming))

        self.assertEqual(len(app._pending_chats), 100)
        self.assertIn("达到 100 条上限", app.wechat.sent[-1][0])

    def test_chatgpt_reply_images_are_sent_to_same_wechat_context(self) -> None:
        app = self.make_app()
        target = ReplyTarget("user-a", "ctx-image")
        app._reply_targets["ilink:bot-1:user-a"] = target

        app._send_event(
            "这是生成的图片",
            "ilink:bot-1:user-a",
            ("D:/runtime/chatgpt-images/reply.png",),
        )

        self.assertEqual(app.wechat.sent, [("这是生成的图片", target)])
        self.assertEqual(
            app.wechat.sent_images,
            [("D:/runtime/chatgpt-images/reply.png", target)],
        )

    def test_chatgpt_export_file_is_sent_to_same_wechat_context(self) -> None:
        app = self.make_app()
        target = ReplyTarget("user-a", "ctx-file")
        app._reply_targets["ilink:bot-1:user-a"] = target

        app._send_event(
            "导出完成",
            "ilink:bot-1:user-a",
            file_paths=("D:/runtime/chatgpt-exports/conversation.md",),
        )

        self.assertEqual(app.wechat.sent, [("导出完成", target)])
        self.assertEqual(
            app.wechat.sent_files,
            [("D:/runtime/chatgpt-exports/conversation.md", target)],
        )

    def test_async_reply_survives_temporary_send_failure(self) -> None:
        app = self.make_app()
        target = ReplyTarget("user-a", "ctx-retry")
        app._reply_targets["ilink:bot-1:user-a"] = target
        app.wechat.send_failures = 1

        app._send_event("最终结果", "ilink:bot-1:user-a")

        self.assertEqual(len(app._outbox), 1)
        self.assertEqual(app._outbox[0].attempts, 1)
        self.assertTrue(app._outbox_file.is_file())
        app._outbox = app._load_outbox()
        app._outbox[0] = replace(app._outbox[0], next_attempt_at=0)
        app._flush_outbox()

        self.assertEqual(app.wechat.sent, [("最终结果", target)])
        self.assertEqual(app._outbox, [])

    def test_async_reply_keeps_original_context_when_later_message_arrives(self) -> None:
        app = self.make_app()
        session_key = "ilink:bot-1:user-a"
        original = ReplyTarget("user-a", "ctx-original")
        later = ReplyTarget("user-a", "ctx-later")
        app._stage_active_job("message-a", session_key, "ChatGPT 请求", original)
        app._reply_targets[session_key] = later

        app._queue_async_result("原请求结果", session_key)
        app._flush_outbox()

        self.assertEqual(app.wechat.sent, [("原请求结果", original)])
        self.assertEqual(app._active_jobs, {})

    def test_completion_backlog_is_retained_until_result_is_durable(self) -> None:
        app = self.make_app()
        session_key = "ilink:bot-1:user-a"
        target = ReplyTarget("user-a", "ctx-completion")
        app._stage_active_job("message-a", session_key, "Codex 任务", target)
        app.runner.events = [SimpleNamespace(text="完成结果", session_key=session_key)]
        app._collect_completion_events()
        persist = app._queue_async_result
        app._queue_async_result = lambda *_args: (_ for _ in ()).throw(OSError("disk"))

        with self.assertRaises(OSError):
            app._persist_completion_backlog()

        self.assertEqual(len(app._completion_backlog), 1)
        app._queue_async_result = persist
        app._persist_completion_backlog()
        self.assertEqual(len(app._completion_backlog), 0)
        self.assertEqual([item.payload for item in app._outbox], ["完成结果"])

    def test_completion_cleanup_retry_does_not_duplicate_delivery(self) -> None:
        app = self.make_app()
        session_key = "ilink:bot-1:user-a"
        target = ReplyTarget("user-a", "ctx-cleanup")
        job = app._stage_active_job("message-a", session_key, "Codex 任务", target)
        app.runner.events = [SimpleNamespace(text="唯一结果", session_key=session_key)]
        app._collect_completion_events()
        save = app._save_active_jobs
        failures = 1

        def fail_once() -> None:
            nonlocal failures
            if failures:
                failures -= 1
                raise OSError("disk")
            save()

        app._save_active_jobs = fail_once
        with self.assertRaises(OSError):
            app._persist_completion_backlog()

        app._persist_completion_backlog()
        app._flush_outbox()

        self.assertEqual(app.wechat.sent, [("唯一结果", target)])
        self.assertEqual(app.wechat.client_ids, [f"{job.job_id}:text"])

    def test_permanent_outbox_failure_does_not_block_later_messages(self) -> None:
        app = self.make_app()
        target = ReplyTarget("user-a", "ctx-outbox")
        missing = str(app.config.runtime_dir / "missing.png")
        app.wechat.fail_image_paths.add(missing)
        app._queue_event("", None, image_paths=(missing,), target=target)
        app._queue_event("后续消息", None, target=target)

        app._flush_outbox()

        self.assertEqual(app.wechat.sent, [("后续消息", target)])
        self.assertEqual(app._outbox, [])
        self.assertEqual(len(app._dead_letters), 1)
        self.assertEqual(app._dead_letters[0].payload, missing)
        self.assertTrue(app._dead_letter_file.is_file())

    def test_outbox_checkpoint_failure_retains_work_with_stable_id(self) -> None:
        app = self.make_app()
        target = ReplyTarget("user-a", "ctx-checkpoint")
        app._queue_event("持久化结果", None, target=target)
        item_id = app._outbox[0].item_id
        save = app._save_outbox
        app._save_outbox = lambda: (_ for _ in ()).throw(OSError("disk"))

        with self.assertRaises(OSError):
            app._flush_outbox()

        self.assertEqual([item.item_id for item in app._outbox], [item_id])
        app._save_outbox = save
        app._flush_outbox()
        self.assertEqual(app.wechat.client_ids, [item_id, item_id])
        self.assertEqual(app._outbox, [])

    def test_transient_outbox_failure_is_not_dead_lettered_after_five_attempts(self) -> None:
        app = self.make_app()
        target = ReplyTarget("user-a", "ctx-long-retry")
        app.wechat.send_failures = 5
        app._queue_event("稍后恢复", None, target=target)

        for _ in range(5):
            app._flush_outbox()
            app._outbox[0] = replace(app._outbox[0], next_attempt_at=0)

        self.assertEqual(app._outbox[0].attempts, 5)
        self.assertEqual(app._dead_letters, [])
        app._flush_outbox()
        self.assertEqual(app.wechat.sent, [("稍后恢复", target)])

    def test_outbox_flush_has_a_bounded_send_batch(self) -> None:
        app = self.make_app()
        target = ReplyTarget("user-a", "ctx-batch")
        for index in range(25):
            app._queue_event(f"消息 {index}", None, target=target)

        app._flush_outbox()

        self.assertEqual(len(app.wechat.sent), 20)
        self.assertEqual(len(app._outbox), 5)

    def test_corrupt_durable_queues_fail_closed_without_overwrite(self) -> None:
        app = self.make_app()
        checks = (
            (app._outbox_file, app._load_outbox),
            (app._active_jobs_file, app._load_active_jobs),
            (app._pending_file, app._load_pending_chats),
        )
        for path, loader in checks:
            with self.subTest(path=path.name):
                path.write_text("{broken", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "避免"):
                    loader()
                self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_legacy_outbox_item_gets_a_stable_migration_id(self) -> None:
        app = self.make_app()
        app._outbox_file.write_text(
            json.dumps(
                [
                    {
                        "kind": "text",
                        "payload": "旧队列回复",
                        "user_id": "user-a",
                        "context_token": "ctx-old",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        first = app._load_outbox()[0].item_id
        second = app._load_outbox()[0].item_id

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("legacy:"))

    def test_runtime_state_is_bound_to_one_ilink_account(self) -> None:
        app = self.make_app()

        app._bind_runtime_account("bot-old")

        with self.assertRaisesRegex(ILinkStateError, "其他 iLink Bot"):
            app._bind_runtime_account("bot-new")
        saved = json.loads(app._runtime_account_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["account_id"], "bot-old")

    def test_corrupt_runtime_account_binding_fails_closed(self) -> None:
        app = self.make_app()
        app._runtime_account_file.write_text("{broken", encoding="utf-8")

        with self.assertRaisesRegex(ILinkStateError, "避免覆盖"):
            app._bind_runtime_account("bot-new")

        self.assertEqual(
            app._runtime_account_file.read_text(encoding="utf-8"), "{broken"
        )

    def test_interrupted_active_job_is_recovered_to_original_context(self) -> None:
        app = self.make_app()
        session_key = "ilink:bot-1:user-a"
        target = ReplyTarget("user-a", "ctx-before-crash")
        job = app._stage_active_job("message-a", session_key, "Codex 任务", target)

        app._recover_active_jobs()

        self.assertEqual(app._active_jobs, {})
        self.assertEqual(len(app._outbox), 1)
        self.assertEqual(app._outbox[0].target, target)
        self.assertEqual(app._outbox[0].job_id, job.job_id)
        self.assertIn("程序中断", app._outbox[0].payload)
        self.assertIn("message-a", app.wechat.acknowledged)

    def test_recovery_drops_a_pending_copy_of_an_already_claimed_job(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "已被领取的排队请求")
        session_key = app._chat_session_key(incoming)
        app.chat_runner.active = True
        app._handle(incoming, incoming.reply_target, session_key)
        self.assertEqual(len(app._pending_chats), 1)
        app._stage_active_job(
            incoming.key,
            session_key,
            "ChatGPT 排队请求",
            incoming.reply_target,
        )

        app._recover_active_jobs()

        self.assertEqual(app._pending_chats, [])
        self.assertEqual(app._load_pending_chats(), [])

    def test_conversation_commands_are_dispatched(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "@归档对话")
        target = incoming.reply_target
        session_key = app._chat_session_key(incoming)

        app._handle(incoming, target, session_key)
        app._active_jobs.clear()
        app._handle(message("user-a", "@重命名对话：发布方案"), target, session_key)
        app._active_jobs.clear()
        app._handle(message("user-a", "@重试"), target, session_key)

        self.assertEqual(app.chat_runner.archive_calls, [session_key])
        self.assertEqual(
            app.chat_runner.rename_calls,
            [(session_key, "发布方案")],
        )
        self.assertEqual(app.chat_runner.retry_calls, [session_key])

    def test_second_stage_conversation_commands_are_dispatched(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "@对话列表")
        target = incoming.reply_target
        session_key = app._chat_session_key(incoming)

        app._handle(incoming, target, session_key)
        app._handle(message("user-a", "@切换对话：2"), target, session_key)
        app._handle(message("user-a", "@导出对话"), target, session_key)
        app._active_jobs.clear()
        app._handle(message("user-a", "@总结对话"), target, session_key)

        self.assertEqual(
            app.chat_runner.switch_calls,
            [(session_key, 2, "微信助手")],
        )
        self.assertEqual(
            app.chat_runner.export_calls,
            [(session_key, "微信助手")],
        )
        self.assertEqual(app.chat_runner.summary_calls, [session_key])
        self.assertIn("最近保存", app.wechat.sent[0][0])

    def test_resend_uses_only_last_successful_reply(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "@重发")
        target = incoming.reply_target
        session_key = app._chat_session_key(incoming)
        app._reply_targets[session_key] = target
        app.chat_runner.last_event = ChatEvent(
            "最后回复",
            session_key,
            ("D:/runtime/chatgpt-images/latest.png",),
        )

        app._handle(incoming, target, session_key)

        self.assertEqual(app.wechat.sent, [("最后回复", target)])
        self.assertEqual(
            app.wechat.sent_images,
            [("D:/runtime/chatgpt-images/latest.png", target)],
        )

    def test_cache_clear_removes_only_old_unprotected_images(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inbound = root / "inbound-images"
            replies = root / "chatgpt-images"
            inbound.mkdir()
            replies.mkdir()
            old = inbound / "old.jpg"
            protected = replies / "protected.png"
            recent = replies / "recent.png"
            old.write_bytes(b"old")
            protected.write_bytes(b"protected")
            recent.write_bytes(b"recent")
            old_time = 1_600_000_000
            os.utime(old, (old_time, old_time))
            os.utime(protected, (old_time, old_time))

            app = self.make_app()
            app.config = SimpleNamespace(
                **{**app.config.__dict__, "runtime_dir": root}
            )
            app.chat_runner.protected_paths = frozenset({protected.resolve()})

            result = app._clear_cache(7)

            self.assertIn("1 个文件", result)
            self.assertFalse(old.exists())
            self.assertTrue(protected.exists())
            self.assertTrue(recent.exists())

    def test_doctor_reports_all_components(self) -> None:
        app = self.make_app()

        result = app._doctor_status()

        self.assertIn("微信 iLink：已连接", result)
        self.assertIn("ChatGPT 浏览器：运行中", result)
        self.assertIn("Codex 当前空闲", result)
        self.assertIn("出站队列：待发送 0，死信 0；入站死信 0", result)
        self.assertIn("图片缓存", result)

    def test_project_switch_changes_default_work_project_and_persists(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.make_app()
            app.config = SimpleNamespace(**{
                **app.config.__dict__,
                "runtime_dir": root,
                "projects": {"control": root, "demo": root / "demo"},
            })
            app._project_selection_file = root / "selected_projects.json"
            incoming = message("owner-id", "@切换项目：demo")
            session_key = app._chat_session_key(incoming)

            app._handle(incoming, incoming.reply_target, session_key)
            app._handle(
                message("owner-id", "@干活：运行测试"),
                incoming.reply_target,
                session_key,
            )

            self.assertEqual(app._selected_projects[session_key], "demo")
            self.assertTrue(app._project_selection_file.is_file())
            self.assertEqual(app.runner.work_calls[0][0], "demo")
            self.assertEqual(app.runner.work_calls[0][1], root / "demo")

    def test_recent_logs_are_tailed_and_secrets_are_redacted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gui.log").write_text(
                "first\n"
                "token=secret-value user-a@im.wechat\n"
                "context_token: another-secret\n"
                '\"aes_key\": \"json-secret\"\n'
                "Authorization: Bearer bearer-secret\n",
                encoding="utf-8",
            )
            app = self.make_app()
            app.config = SimpleNamespace(
                **{**app.config.__dict__, "runtime_dir": root}
            )

            result = app._recent_logs(4)

            self.assertNotIn("secret-value", result)
            self.assertNotIn("another-secret", result)
            self.assertNotIn("json-secret", result)
            self.assertNotIn("bearer-secret", result)
            self.assertNotIn("user-a@im.wechat", result)
            self.assertIn("[已隐藏]", result)
            self.assertIn("[微信ID]", result)

    def test_recovery_commands_require_codex_permission(self) -> None:
        app = self.make_app()
        app.config = SimpleNamespace(**{
            **app.config.__dict__,
            "ilink_allowed_user_ids": frozenset({"user-a"}),
            "ilink_codex_user_ids": frozenset({"owner-id"}),
        })
        incoming = message("user-a", "@重启浏览器")

        app._handle(incoming, incoming.reply_target, app._chat_session_key(incoming))

        self.assertEqual(app.chat_runner.restart_calls, 0)
        self.assertEqual(app.wechat.sent[-1][0], "无权限执行 Codex 操作")

    def test_authorized_recovery_commands_restart_connections(self) -> None:
        app = self.make_app()
        reconnect = message("owner-id", "@重连微信")
        restart = message("owner-id", "@重启浏览器")

        app._handle(reconnect, reconnect.reply_target, app._chat_session_key(reconnect))
        app._handle(restart, restart.reply_target, app._chat_session_key(restart))

        self.assertEqual(app.wechat.reconnect_calls, 1)
        self.assertEqual(app.chat_runner.restart_calls, 1)


if __name__ == "__main__":
    unittest.main()
