from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from wechat_codex.config import default_config_path, load_config


class ConfigTests(unittest.TestCase):
    def test_frozen_app_uses_config_beside_executable(self) -> None:
        executable = Path("C:/Program Files/WeChat Codex/wechat-codex-gui.exe")
        with patch("wechat_codex.config.sys.frozen", True, create=True), patch(
            "wechat_codex.config.sys.executable", str(executable)
        ):
            self.assertEqual(
                default_config_path(),
                executable.resolve().parent / "config.yaml",
            )

    def _write_config(self, root: Path, extra: str = "") -> Path:
        source = root / "config.yaml"
        source.write_text(
            f"{extra}projects:\n  demo: .\n",
            encoding="utf-8",
        )
        return source

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
            self.assertFalse(config.send_received_ack)
            self.assertEqual(config.chatgpt_browser_channel, "chrome")
            self.assertTrue(config.chatgpt_headless)
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

    def test_quoted_boolean_values_are_parsed_instead_of_treated_as_truthy(self) -> None:
        with TemporaryDirectory() as directory:
            source = self._write_config(
                Path(directory),
                'send_received_ack: "false"\nchatgpt_headless: "false"\n',
            )

            config = load_config(source)

            self.assertFalse(config.send_received_ack)
            self.assertFalse(config.chatgpt_headless)

    def test_rejects_non_boolean_values(self) -> None:
        for key, value in (
            ("send_received_ack", '"disabled"'),
            ("chatgpt_headless", "1"),
        ):
            with self.subTest(key=key), TemporaryDirectory() as directory:
                source = self._write_config(Path(directory), f"{key}: {value}\n")

                with self.assertRaisesRegex(ValueError, key):
                    load_config(source)

    def test_poll_seconds_must_be_finite_and_reasonably_bounded(self) -> None:
        for value in (".nan", ".inf", "60.1"):
            with self.subTest(value=value), TemporaryDirectory() as directory:
                source = self._write_config(
                    Path(directory), f"poll_seconds: {value}\n"
                )

                with self.assertRaisesRegex(ValueError, "poll_seconds"):
                    load_config(source)

    def test_codex_users_must_also_be_allowed_message_senders(self) -> None:
        with TemporaryDirectory() as directory:
            source = self._write_config(
                Path(directory),
                "ilink_allowed_user_ids: [user-a]\n"
                "ilink_codex_user_ids: [user-a, user-b]\n",
            )

            with self.assertRaisesRegex(ValueError, "user-b"):
                load_config(source)

    def test_permission_lists_only_accept_nonempty_user_id_strings(self) -> None:
        with TemporaryDirectory() as directory:
            source = self._write_config(
                Path(directory), "ilink_allowed_user_ids: [user-a, 123]\n"
            )

            with self.assertRaisesRegex(ValueError, "第 2 项"):
                load_config(source)

    def test_rejects_invalid_proxy_urls(self) -> None:
        for proxy in ("file:///tmp/proxy", "http://:7890", "http://proxy:bad"):
            with self.subTest(proxy=proxy), TemporaryDirectory() as directory:
                source = self._write_config(
                    Path(directory), f'chatgpt_proxy_server: "{proxy}"\n'
                )

                with self.assertRaisesRegex(ValueError, "chatgpt_proxy_server"):
                    load_config(source)


if __name__ == "__main__":
    unittest.main()
