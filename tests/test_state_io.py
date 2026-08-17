from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from wechat_codex.state_io import atomic_write_text


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_replaces_payload_and_removes_temporary_file(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("old", encoding="utf-8")

            atomic_write_text(path, "new\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_replace_failure_preserves_original_and_cleans_temporary_file(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("old", encoding="utf-8")
            with patch("wechat_codex.state_io.os.replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    atomic_write_text(path, "new")

            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
