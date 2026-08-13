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
            self.assertEqual(config.bot_name, "ChatGpt机器人")
            self.assertEqual(config.authorized_senders, frozenset({"無惧"}))
            self.assertTrue(config.background_mode)
            self.assertTrue(config.voice_recognition)
            self.assertEqual(config.voice_retry_count, 3)
            self.assertEqual(config.chat_model, "gpt-5.6-terra")
            self.assertEqual(config.chat_reasoning_effort, "low")
            self.assertEqual(config.chat_max_output_tokens, 1200)
            self.assertEqual(config.projects["demo"], (root / "repo").resolve())


if __name__ == "__main__":
    unittest.main()
