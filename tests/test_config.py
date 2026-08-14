from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from wechat_codex.config import load_config


class ConfigTests(unittest.TestCase):
    def test_defaults_and_relative_paths_are_resolved_from_config(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            source = root / "config.yaml"
            source.write_text(
                "default_project: demo\nprojects:\n  demo: repo\n",
                encoding="utf-8",
            )

            config = load_config(source)

            self.assertEqual(config.ilink_api_base_url, "https://ilinkai.weixin.qq.com")
            self.assertEqual(
                config.ilink_credentials_file, (root / ".runtime/ilink-account.json").resolve()
            )
            self.assertEqual(config.ilink_allowed_user_ids, frozenset())
            self.assertEqual(config.ilink_codex_user_ids, frozenset())
            self.assertTrue(config.send_received_ack)
            self.assertEqual(config.chatgpt_browser_channel, "chrome")
            self.assertFalse(config.chatgpt_headless)
            self.assertEqual(config.chatgpt_conversation_title, "微信助手")
            self.assertEqual(config.projects["demo"], (root / "repo").resolve())

    def test_user_lists_proxy_and_custom_state_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.yaml"
            source.write_text(
                "default_project: demo\n"
                "ilink_allowed_user_ids: [user-a, user-b]\n"
                "ilink_codex_user_ids: user-a\n"
                "ilink_state_file: state/data.json\n"
                "chatgpt_proxy_server: 127.0.0.1:7890\n"
                "chatgpt_conversation_title: 我的微信对话\n"
                "projects:\n  demo: .\n",
                encoding="utf-8",
            )

            config = load_config(source)

            self.assertEqual(config.ilink_allowed_user_ids, frozenset({"user-a", "user-b"}))
            self.assertEqual(config.ilink_codex_user_ids, frozenset({"user-a"}))
            self.assertEqual(config.ilink_state_file, (root / "state/data.json").resolve())
            self.assertEqual(config.chatgpt_proxy_server, "http://127.0.0.1:7890")
            self.assertEqual(config.chatgpt_conversation_title, "我的微信对话")

    def test_rejects_non_https_ilink_base_url(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "config.yaml"
            source.write_text(
                "ilink_api_base_url: http://example.test\nprojects:\n  demo: .\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                load_config(source)


if __name__ == "__main__":
    unittest.main()
