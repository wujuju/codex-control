import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from wechat_codex.config import load_config


class ConfigTests(unittest.TestCase):
    def test_bot_config_and_relative_project(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            config_file = root / "config.yaml"
            config_file.write_text(
                """
wecom_bot_id: bot-test
group_only: true
allowed_group_chat_ids: [group-chat-id]
authorized_senders: [zhangsan]
default_group_chat_name: ChatGpt
group_chat_names: {group-chat-id: 项目群}
user_chat_names: {zhangsan: 张三}
default_project: demo
projects:
  demo: repo
""".strip(),
                encoding="utf-8",
            )
            config = load_config(config_file)
            self.assertEqual(config.wecom_bot_id, "bot-test")
            self.assertTrue(config.group_only)
            self.assertEqual(
                config.allowed_group_chat_ids, frozenset({"group-chat-id"})
            )
            self.assertEqual(config.chatgpt_browser_channel, "chrome")
            self.assertTrue(config.chatgpt_headless)
            self.assertEqual(config.default_group_chat_name, "ChatGpt")
            self.assertEqual(config.group_chat_names, {"group-chat-id": "项目群"})
            self.assertEqual(config.user_chat_names, {"zhangsan": "张三"})
            self.assertEqual(config.authorized_senders, frozenset({"zhangsan"}))
            self.assertEqual(config.projects["demo"], (root / "repo").resolve())

    def test_secrets_are_read_from_environment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            config_file = root / "config.yaml"
            config_file.write_text(
                "wecom_bot_id: bot-test\n"
                "authorized_senders: [u1]\nprojects: {demo: repo}\n",
                encoding="utf-8",
            )
            config = load_config(config_file)
            with patch.dict(os.environ, {"WECOM_BOT_SECRET": "secret"}):
                self.assertEqual(config.wecom_bot_secret, "secret")


if __name__ == "__main__":
    unittest.main()
