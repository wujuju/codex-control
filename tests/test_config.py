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
            self.assertEqual(config.projects["demo"], (root / "repo").resolve())


if __name__ == "__main__":
    unittest.main()

