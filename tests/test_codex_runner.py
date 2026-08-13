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
            runner = CodexRunner("codex", Path(directory), 30, 30)
            args = runner.build_work_args(Path(directory), "任务")
        self.assertIn("workspace-write", args)
        self.assertIn("--json", args)

    def test_chat_command_is_ephemeral_and_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            runner = CodexRunner("codex", Path(directory), 30, 30)
            args = runner.build_chat_args("你好")
        self.assertIn("--ephemeral", args)
        self.assertIn("read-only", args)


if __name__ == "__main__":
    unittest.main()

