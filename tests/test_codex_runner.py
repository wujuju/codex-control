import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import time
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

    def test_recent_tasks_persist_results_and_are_session_isolated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            (project / ".git").mkdir(parents=True)
            runner = CodexRunner(
                "wechat-codex-command-that-does-not-exist",
                root,
                30,
            )

            self.assertTrue(
                runner.begin_work(
                    "demo",
                    project,
                    "运行测试",
                    "friend:甲",
                )[0]
            )
            deadline = time.monotonic() + 2
            while runner.active and time.monotonic() < deadline:
                time.sleep(0.01)

            own = runner.recent_tasks("friend:甲")
            other = runner.recent_tasks("friend:乙")
            saved = json.loads(
                (root / "codex_task_history.json").read_text(encoding="utf-8")
            )

            self.assertFalse(runner.active)
            self.assertIn("运行测试", own)
            self.assertIn("[失败]", own)
            self.assertEqual(other, "当前没有保存的 Codex 任务")
            self.assertEqual(saved[0]["session_key"], "friend:甲")
            self.assertEqual(saved[0]["status"], "失败")

    def test_subprocess_jsonl_is_streamed_into_result(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runner = CodexRunner("codex", root, 30)
            script = (
                "import json; "
                "print(json.dumps({'type':'thread.started','thread_id':'stream-1'})); "
                "print(json.dumps({'type':'item.completed','item':"
                "{'type':'agent_message','text':'流式完成'}}, ensure_ascii=False))"
            )

            self.assertTrue(
                runner._begin(
                    [sys.executable, "-c", script],
                    "work",
                    "demo",
                    root,
                    10,
                    "friend:甲",
                    "流式测试",
                )[0]
            )
            deadline = time.monotonic() + 5
            while runner.active and time.monotonic() < deadline:
                time.sleep(0.01)

            events = runner.drain_events()
            self.assertEqual([event.text for event in events], ["流式完成"])
            self.assertIn("[成功]", runner.recent_tasks("friend:甲"))


if __name__ == "__main__":
    unittest.main()
