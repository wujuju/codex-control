from types import SimpleNamespace
import unittest

from wechat_codex.app import BridgeApp
from wechat_codex.wechat_client import IncomingMessage


class FakeWeChat:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class FakeRunner:
    def __init__(self) -> None:
        self.work_calls = 0
        self.active = False

    def begin_work(self, *args):
        self.work_calls += 1
        return True, "已开始"


class FakeChatRunner:
    def __init__(self) -> None:
        self.active = False
        self.chat_calls: list[tuple[str, str, str | None]] = []

    def begin_chat(
        self,
        session_key: str,
        prompt: str,
        conversation_title: str | None = None,
    ):
        self.chat_calls.append((session_key, prompt, conversation_title))
        return True, "已开始"


class BridgeAppTests(unittest.TestCase):
    def test_incoming_message_is_acknowledged_before_handling(self) -> None:
        app = BridgeApp.__new__(BridgeApp)
        app._event_sink = None
        app._state_sink = None
        app.config = SimpleNamespace(
            send_received_ack=True,
            received_ack_text="已收到，正在处理中，请稍等…",
        )
        calls: list[object] = []
        app._send = lambda text: calls.append(("send", text))
        app._handle = lambda message: calls.append(("handle", message.key))
        message = IncomingMessage(
            key="incoming-1",
            content="帮我处理",
            sender="無惧",
            attr="friend",
        )

        app._process_incoming(message)

        self.assertEqual(
            calls,
            [
                ("send", "已收到，正在处理中，请稍等…"),
                ("handle", "incoming-1"),
            ],
        )

    def test_self_message_does_not_get_received_ack(self) -> None:
        app = BridgeApp.__new__(BridgeApp)
        app.config = SimpleNamespace(
            send_received_ack=True,
            received_ack_text="已收到，正在处理中，请稍等…",
        )
        sent: list[str] = []
        app._send = sent.append

        app._acknowledge(
            IncomingMessage(
                key="self-1",
                content="自己的消息",
                sender="he yang",
                attr="self",
            )
        )

        self.assertEqual(sent, [])

    def test_unauthorized_sender_cannot_start_work(self) -> None:
        app = BridgeApp.__new__(BridgeApp)
        app._event_sink = None
        app._state_sink = None
        app.config = SimpleNamespace(
            authorized_senders=frozenset({"無惧"}),
            bot_name="ChatGpt机器人",
            projects={"control": "."},
            default_project="control",
        )
        app.wechat = FakeWeChat()
        app.runner = FakeRunner()
        app.chat_runner = FakeChatRunner()

        app._handle(
            IncomingMessage(
                key="1",
                content="干活：运行测试",
                sender="其他人",
                attr="friend",
            )
        )

        self.assertEqual(app.runner.work_calls, 0)
        self.assertEqual(app.wechat.sent, ["无权限执行 Codex 操作"])

    def test_group_chat_uses_member_specific_persistent_session(self) -> None:
        app = BridgeApp.__new__(BridgeApp)
        app._event_sink = None
        app._state_sink = None
        app.config = SimpleNamespace(
            authorized_senders=frozenset({"無惧"}),
            bot_name="ChatGpt机器人",
            contact="测试群",
            chatgpt_conversation_title_prefix="微信",
            projects={"control": "."},
            default_project="control",
        )
        app.wechat = FakeWeChat()
        app.runner = FakeRunner()
        app.chat_runner = FakeChatRunner()

        app._handle(
            IncomingMessage(
                key="1",
                content="你好",
                sender="张三",
                attr="friend",
                conversation="测试群",
                chat_type="group",
            )
        )

        self.assertEqual(
            app.chat_runner.chat_calls,
            [("group:测试群:张三", "你好", "微信群测试群-张三")],
        )

    def test_private_chat_uses_contact_title(self) -> None:
        app = BridgeApp.__new__(BridgeApp)
        app._event_sink = None
        app._state_sink = None
        app.config = SimpleNamespace(
            authorized_senders=frozenset({"無惧"}),
            bot_name="ChatGpt机器人",
            contact="無惧",
            chatgpt_conversation_title_prefix="微信",
            projects={"control": "."},
            default_project="control",
        )
        app.wechat = FakeWeChat()
        app.runner = FakeRunner()
        app.chat_runner = FakeChatRunner()

        app._handle(
            IncomingMessage(
                key="1",
                content="你好",
                sender="無惧",
                attr="friend",
                conversation="無惧",
                chat_type="friend",
            )
        )

        self.assertEqual(
            app.chat_runner.chat_calls,
            [("friend:無惧", "你好", "微信無惧")],
        )


if __name__ == "__main__":
    unittest.main()
