from types import SimpleNamespace
import unittest

from wechat_codex.app import BridgeApp
from wechat_codex.ilink_client import IncomingMessage, ReplyTarget


class FakeWeChat:
    def __init__(self) -> None:
        self.user_id = "owner-id"
        self.sent: list[tuple[str, ReplyTarget]] = []
        self.sent_images: list[tuple[str, ReplyTarget]] = []

    def send(self, text: str, target: ReplyTarget) -> None:
        self.sent.append((text, target))

    def send_image(self, image_path: str, target: ReplyTarget) -> None:
        self.sent_images.append((image_path, target))


class FakeRunner:
    def __init__(self) -> None:
        self.work_calls: list[tuple] = []
        self.active = False

    def begin_work(self, *args):
        self.work_calls.append(args)
        return True, "已开始"


class FakeChatRunner:
    def __init__(self) -> None:
        self.active = False
        self.chat_calls: list[tuple[str, str, str | None, tuple[str, ...]]] = []

    def begin_chat(
        self, session_key, prompt, conversation_title=None, image_paths=()
    ):
        self.chat_calls.append((session_key, prompt, conversation_title, image_paths))
        return True, "已开始"


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
        )
        app.wechat = FakeWeChat()
        app.runner = FakeRunner()
        app.chat_runner = FakeChatRunner()
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
        incoming = message("user-a", "干活：运行测试")
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


if __name__ == "__main__":
    unittest.main()
