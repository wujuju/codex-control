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
        self.chat_calls: list[tuple[str, str]] = []

    def begin_chat(self, session_key: str, prompt: str):
        self.chat_calls.append((session_key, prompt))
        return True, "已开始"


class BridgeAppTests(unittest.TestCase):
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
            [("group:测试群:张三", "你好")],
        )


if __name__ == "__main__":
    unittest.main()
