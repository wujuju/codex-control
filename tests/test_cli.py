import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wechat_codex.cli import read_messages
from wechat_codex.ilink_client import IncomingMessage


class ReadMessagesTests(unittest.TestCase):
    def test_read_command_acknowledges_printed_message(self) -> None:
        calls: list[str] = []

        class FakeClient:
            def connect(self) -> None:
                calls.append("connect")

            def poll(self):
                calls.append("poll")
                if calls.count("poll") == 1:
                    return [IncomingMessage("ilink:1", "你好", "user-a", sender_id="user-a")]
                raise KeyboardInterrupt

            def acknowledge(self, key: str) -> None:
                calls.append(f"ack:{key}")

            def close(self) -> None:
                calls.append("close")

        output = io.StringIO()
        config = SimpleNamespace(poll_seconds=0)
        with patch("wechat_codex.cli._ilink_client", return_value=FakeClient()), patch(
            "sys.stdout", output
        ):
            result = read_messages(config)

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["connect", "poll", "ack:ilink:1", "poll", "close"])
        self.assertIn('"content": "你好"', output.getvalue())
        self.assertIn("已停止读取消息", output.getvalue())


if __name__ == "__main__":
    unittest.main()
