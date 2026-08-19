import io
from contextlib import nullcontext
from pathlib import Path
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from wechat_codex.cli import main, read_messages
from wechat_codex.config import AppConfig
from wechat_codex.ilink_client import IncomingMessage
from wechat_codex.instance_lock import AlreadyRunningError


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
        config = cast(AppConfig, SimpleNamespace(poll_seconds=0))
        with patch("wechat_codex.cli._ilink_client", return_value=FakeClient()), patch(
            "sys.stdout", output
        ):
            result = read_messages(config)

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["connect", "poll", "ack:ilink:1", "poll", "close"])
        self.assertIn('"content": "你好"', output.getvalue())
        self.assertIn("已停止读取消息", output.getvalue())


class MainInstanceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = cast(
            AppConfig,
            SimpleNamespace(
                runtime_dir=Path(".runtime"),
                chatgpt_profile_dir=Path("profile"),
                chatgpt_browser_channel="chrome",
                chatgpt_proxy_server=None,
                ilink_credentials_file=Path("account.json"),
                ilink_api_base_url="https://ilinkai.weixin.qq.com",
            ),
        )

    def test_start_holds_runtime_lock_while_bridge_runs(self) -> None:
        calls: list[str] = []

        class FakeLock:
            def __enter__(self):
                calls.append("lock")

            def __exit__(self, *_args):
                calls.append("unlock")

        class FakeBridge:
            def __init__(self, _config) -> None:
                pass

            def run(self) -> None:
                calls.append("run")

        with patch("wechat_codex.cli.load_config", return_value=self.config), patch(
            "wechat_codex.cli.runtime_instance_lock", return_value=FakeLock()
        ) as runtime_lock, patch("wechat_codex.cli.BridgeApp", FakeBridge):
            result = main(["--config", "config.yaml", "start", "--skip-login-check"])

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["lock", "run", "unlock"])
        runtime_lock.assert_called_once_with(self.config.runtime_dir)

    def test_read_holds_the_same_runtime_lock(self) -> None:
        calls: list[str] = []

        class FakeLock:
            def __enter__(self):
                calls.append("lock")

            def __exit__(self, *_args):
                calls.append("unlock")

        with patch("wechat_codex.cli.load_config", return_value=self.config), patch(
            "wechat_codex.cli.runtime_instance_lock", return_value=FakeLock()
        ) as runtime_lock, patch(
            "wechat_codex.cli.read_messages",
            side_effect=lambda _config: calls.append("read") or 0,
        ):
            result = main(["--config", "config.yaml", "read"])

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["lock", "read", "unlock"])
        runtime_lock.assert_called_once_with(self.config.runtime_dir)

    def test_setup_releases_runtime_lock_after_login(self) -> None:
        calls: list[str] = []

        class FakeLock:
            def __enter__(self):
                calls.append("lock")

            def __exit__(self, *_args):
                calls.append("unlock")

        with patch("wechat_codex.cli.load_config", return_value=self.config), patch(
            "wechat_codex.cli.runtime_instance_lock", return_value=FakeLock()
        ) as runtime_lock, patch(
            "wechat_codex.cli.ensure_logins",
            side_effect=lambda _config: calls.append("setup"),
        ):
            result = main(["--config", "config.yaml", "setup"])

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["lock", "setup", "unlock"])
        runtime_lock.assert_called_once_with(self.config.runtime_dir)

    def test_gui_setup_only_checks_shared_chatgpt_login(self) -> None:
        with patch(
            "wechat_codex.cli.load_config", return_value=self.config
        ), patch(
            "wechat_codex.cli.runtime_instance_lock", return_value=nullcontext()
        ), patch(
            "wechat_codex.cli.ensure_chatgpt_login"
        ) as ensure_chatgpt, patch(
            "wechat_codex.cli.ensure_logins"
        ) as ensure_all:
            result = main(
                ["--config", "config.yaml", "setup", "--chatgpt-only"]
            )

        self.assertEqual(result, 0)
        ensure_chatgpt.assert_called_once_with(self.config)
        ensure_all.assert_not_called()

    def test_doctor_does_not_take_runtime_lock(self) -> None:
        with patch("wechat_codex.cli.load_config", return_value=self.config), patch(
            "wechat_codex.cli.runtime_instance_lock"
        ) as runtime_lock, patch("wechat_codex.cli.doctor", return_value=0) as doctor:
            result = main(["--config", "config.yaml", "doctor"])

        self.assertEqual(result, 0)
        doctor.assert_called_once_with(self.config, False)
        runtime_lock.assert_not_called()

    def test_doctor_connect_takes_runtime_lock(self) -> None:
        with patch("wechat_codex.cli.load_config", return_value=self.config), patch(
            "wechat_codex.cli.runtime_instance_lock", return_value=nullcontext()
        ) as runtime_lock, patch(
            "wechat_codex.cli.doctor", return_value=0
        ) as doctor:
            result = main(["--config", "config.yaml", "doctor", "--connect"])

        self.assertEqual(result, 0)
        doctor.assert_called_once_with(self.config, True)
        runtime_lock.assert_called_once_with(self.config.runtime_dir)

    def test_account_login_commands_take_runtime_lock(self) -> None:
        cases = (
            ("chatgpt-login", "wechat_codex.chatgpt_runner.login_chatgpt"),
            ("ilink-login", "wechat_codex.cli.login_with_qr"),
        )
        for command, action_target in cases:
            with self.subTest(command=command), patch(
                "wechat_codex.cli.load_config", return_value=self.config
            ), patch(
                "wechat_codex.cli.runtime_instance_lock",
                return_value=nullcontext(),
            ) as runtime_lock, patch(action_target) as action:
                result = main(["--config", "config.yaml", command])

            self.assertEqual(result, 0)
            action.assert_called_once()
            runtime_lock.assert_called_once_with(self.config.runtime_dir)

    def test_send_takes_runtime_lock_while_using_ilink_state(self) -> None:
        calls: list[str] = []

        class FakeClient:
            def connect(self) -> None:
                calls.append("connect")

            def default_target(self):
                return SimpleNamespace(user_id="owner")

            def send(self, text, target) -> None:
                calls.append(f"send:{text}:{target.user_id}")

            def close(self) -> None:
                calls.append("close")

        with patch("wechat_codex.cli.load_config", return_value=self.config), patch(
            "wechat_codex.cli.runtime_instance_lock", return_value=nullcontext()
        ) as runtime_lock, patch(
            "wechat_codex.cli._ilink_client", return_value=FakeClient()
        ):
            result = main(["--config", "config.yaml", "send", "测试"])

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["connect", "send:测试:owner", "close"])
        runtime_lock.assert_called_once_with(self.config.runtime_dir)

    def test_second_cli_instance_prints_clear_chinese_error(self) -> None:
        error = io.StringIO()
        with patch("wechat_codex.cli.load_config", return_value=self.config), patch(
            "wechat_codex.cli.runtime_instance_lock",
            side_effect=AlreadyRunningError(Path("wechat-codex.lock")),
        ), patch("sys.stderr", error):
            result = main(["--config", "config.yaml", "read"])

        self.assertEqual(result, 1)
        self.assertIn("已有微信 Codex 实例正在运行", error.getvalue())


if __name__ == "__main__":
    unittest.main()
