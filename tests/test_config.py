from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from wechat_codex.config import load_config


class ConfigTests(unittest.TestCase):
    def test_relative_project_is_resolved_from_config_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            config_file = root / "config.yaml"
            config_file.write_text(
                "contact: 测试\ndefault_project: demo\nprojects:\n  demo: repo\n",
                encoding="utf-8",
            )

            config = load_config(config_file)

            self.assertEqual(config.contact, "测试")
            self.assertEqual(config.chat_type, "friend")
            self.assertEqual(config.wechat_message_source, "uia")
            self.assertIsNone(config.wx_cli_path)
            self.assertIsNone(config.wx_cli_username)
            self.assertEqual(config.wx_cli_timeout_seconds, 30.0)
            self.assertEqual(config.bot_name, "ChatGpt机器人")
            self.assertEqual(config.authorized_senders, frozenset({"無惧"}))
            self.assertTrue(config.background_mode)
            self.assertTrue(config.voice_recognition)
            self.assertEqual(config.voice_retry_count, 3)
            self.assertEqual(config.chatgpt_browser_channel, "msedge")
            self.assertTrue(config.chatgpt_headless)
            self.assertIsNone(config.chatgpt_proxy_server)
            self.assertEqual(config.projects["demo"], (root / "repo").resolve())

    def test_chatgpt_proxy_server_gets_default_scheme(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            config_file = root / "config.yaml"
            config_file.write_text(
                "contact: 测试\ndefault_project: demo\n"
                "chatgpt_proxy_server: 127.0.0.1:7890\n"
                "projects:\n  demo: repo\n",
                encoding="utf-8",
            )

            config = load_config(config_file)

            self.assertEqual(
                config.chatgpt_proxy_server,
                "http://127.0.0.1:7890",
            )

    def test_wx_cli_message_source_options(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            config_file = root / "config.yaml"
            config_file.write_text(
                "contact: 测试\ndefault_project: demo\n"
                "wechat_message_source: wx-cli\n"
                "wx_cli_path: C:\\Tools\\wx.exe\n"
                "wx_cli_username: wxid_target\n"
                "wx_cli_timeout_seconds: 12\n"
                "projects:\n  demo: repo\n",
                encoding="utf-8",
            )

            config = load_config(config_file)

            self.assertEqual(config.wechat_message_source, "wx_cli")
            self.assertEqual(config.wx_cli_path, r"C:\Tools\wx.exe")
            self.assertEqual(config.wx_cli_username, "wxid_target")
            self.assertEqual(config.wx_cli_timeout_seconds, 12.0)


if __name__ == "__main__":
    unittest.main()
