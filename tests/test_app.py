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

    def begin_work(self, *args):
        self.work_calls += 1
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


if __name__ == "__main__":
    unittest.main()
