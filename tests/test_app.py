import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from wechat_codex.app import BridgeApp
from wechat_codex.chatgpt_runner import ChatEvent
from wechat_codex.ilink_client import IncomingMessage, ReplyTarget


class FakeWeChat:
    def __init__(self) -> None:
        self.user_id = "owner-id"
        self.connected = True
        self.sent: list[tuple[str, ReplyTarget]] = []
        self.sent_images: list[tuple[str, ReplyTarget]] = []
        self.sent_files: list[tuple[str, ReplyTarget]] = []
        self.reconnect_calls = 0
        self.send_failures = 0

    def send(self, text: str, target: ReplyTarget) -> None:
        if self.send_failures:
            self.send_failures -= 1
            raise OSError("temporary send failure")
        self.sent.append((text, target))

    def send_image(self, image_path: str, target: ReplyTarget) -> None:
        self.sent_images.append((image_path, target))

    def send_file(self, file_path: str, target: ReplyTarget) -> None:
        self.sent_files.append((file_path, target))

    def reconnect(self) -> None:
        self.reconnect_calls += 1


class FakeRunner:
    def __init__(self) -> None:
        self.work_calls: list[tuple] = []
        self.active = False

    def begin_work(self, *args):
        self.work_calls.append(args)
        return True, "已开始"

    def status(self) -> str:
        return "Codex 当前空闲"

    def recent_tasks(self, session_key=None) -> str:
        return "最近 Codex 任务：\n1. [成功] control：测试"


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


def message(sender: str = "owner-id", content: str = "你好") -> IncomingMessage:
    return IncomingMessage(
        key="ilink:1",
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
        app.wechat = FakeWeChat()
        app.runner = FakeRunner()
        app.chat_runner = FakeChatRunner()
        app._project_selection_file = app.config.runtime_dir / "selected_projects.json"
        app._selected_projects = {}
        app._outbox_file = app.config.runtime_dir / "outbound_queue.json"
        app._outbox = []
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
        incoming = message("user-a", "/干活：运行测试")
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

        with self.assertRaises(OSError):
            app._send_event("最终结果", "ilink:bot-1:user-a")

        self.assertEqual(len(app._outbox), 1)
        self.assertTrue(app._outbox_file.is_file())
        app._outbox = app._load_outbox()
        app._flush_outbox()

        self.assertEqual(app.wechat.sent, [("最终结果", target)])
        self.assertEqual(app._outbox, [])

    def test_conversation_commands_are_dispatched(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "/归档对话")
        target = incoming.reply_target
        session_key = app._chat_session_key(incoming)

        app._handle(incoming, target, session_key)
        app._handle(message("user-a", "/重命名对话：发布方案"), target, session_key)
        app._handle(message("user-a", "/重试"), target, session_key)

        self.assertEqual(app.chat_runner.archive_calls, [session_key])
        self.assertEqual(
            app.chat_runner.rename_calls,
            [(session_key, "发布方案")],
        )
        self.assertEqual(app.chat_runner.retry_calls, [session_key])

    def test_second_stage_conversation_commands_are_dispatched(self) -> None:
        app = self.make_app()
        incoming = message("user-a", "/对话列表")
        target = incoming.reply_target
        session_key = app._chat_session_key(incoming)

        app._handle(incoming, target, session_key)
        app._handle(message("user-a", "/切换对话：2"), target, session_key)
        app._handle(message("user-a", "/导出对话"), target, session_key)
        app._handle(message("user-a", "/总结对话"), target, session_key)

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
        incoming = message("user-a", "/重发")
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
            incoming = message("owner-id", "/切换项目：demo")
            session_key = app._chat_session_key(incoming)

            app._handle(incoming, incoming.reply_target, session_key)
            app._handle(
                message("owner-id", "/干活：运行测试"),
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
                "context_token: another-secret\n",
                encoding="utf-8",
            )
            app = self.make_app()
            app.config = SimpleNamespace(
                **{**app.config.__dict__, "runtime_dir": root}
            )

            result = app._recent_logs(2)

            self.assertNotIn("secret-value", result)
            self.assertNotIn("another-secret", result)
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
        incoming = message("user-a", "/重启浏览器")

        app._handle(incoming, incoming.reply_target, app._chat_session_key(incoming))

        self.assertEqual(app.chat_runner.restart_calls, 0)
        self.assertEqual(app.wechat.sent[-1][0], "无权限执行 Codex 操作")

    def test_authorized_recovery_commands_restart_connections(self) -> None:
        app = self.make_app()
        reconnect = message("owner-id", "/重连微信")
        restart = message("owner-id", "/重启浏览器")

        app._handle(reconnect, reconnect.reply_target, app._chat_session_key(reconnect))
        app._handle(restart, restart.reply_target, app._chat_session_key(restart))

        self.assertEqual(app.wechat.reconnect_calls, 1)
        self.assertEqual(app.chat_runner.restart_calls, 1)


if __name__ == "__main__":
    unittest.main()
