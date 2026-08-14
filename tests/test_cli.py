import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wechat_codex.cli import read_messages


class ReadMessagesTests(unittest.TestCase):
    def test_read_command_baselines_then_only_polls(self) -> None:
        calls: list[str] = []

        class FakeClient:
            def connect(self) -> None:
                calls.append("connect")

            def baseline(self) -> int:
                calls.append("baseline")
                return 4

            def poll(self):
                calls.append("poll")
                raise KeyboardInterrupt

            def send(self, _text: str) -> None:
                raise AssertionError("read command must never send")

        output = io.StringIO()
        config = SimpleNamespace(poll_seconds=1.0)
        with patch(
            "wechat_codex.cli._wechat_client",
            return_value=FakeClient(),
        ) as client_factory, patch("sys.stdout", output):
            result = read_messages(config)

        self.assertEqual(result, 0)
        client_factory.assert_called_once_with(config, use_all_read_filters=True)
        self.assertEqual(calls, ["connect", "baseline", "poll"])
        self.assertIn("已忽略 4 条", output.getvalue())
        self.assertIn("已停止读取消息", output.getvalue())


if __name__ == "__main__":
    unittest.main()
