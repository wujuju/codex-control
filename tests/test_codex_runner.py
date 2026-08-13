from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from wechat_codex.codex_runner import CodexRunner, parse_jsonl


class CodexRunnerTests(unittest.TestCase):
    def test_parse_jsonl(self) -> None:
        output = parse_jsonl(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"完成"}}',
                '{"type":"turn.completed"}',
            ]
        )
        self.assertEqual(output.thread_id, "thread-1")
        self.assertEqual(output.final_text, "完成")

    def test_work_command_has_workspace_sandbox(self) -> None:
        with TemporaryDirectory() as directory:
            runner = CodexRunner("codex", Path(directory), 30)
            args = runner.build_work_args(Path(directory), "任务")
        self.assertIn("workspace-write", args)
        self.assertIn("--json", args)


if __name__ == "__main__":
    unittest.main()
