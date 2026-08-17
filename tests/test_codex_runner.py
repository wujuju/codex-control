import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import time
import unittest
from unittest.mock import patch

from wechat_codex.codex_runner import CodexRunner, parse_jsonl


class CodexRunnerTests(unittest.TestCase):
    def wait_until_idle(self, runner: CodexRunner, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while runner.active and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(runner.active, "CodexRunner did not become idle in time")

    def wait_for_file(self, path: Path, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"file was not created in time: {path}")

    def descendant_process_script(
        self,
        ready_file: Path,
        marker_file: Path,
        marker_delay: float,
    ) -> str:
        child_script = (
            "import time; from pathlib import Path; "
            f"time.sleep({marker_delay!r}); "
            f"Path({str(marker_file)!r}).write_text('alive', encoding='utf-8')"
        )
        return (
            "import subprocess, sys, time; from pathlib import Path; "
            f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
            f"Path({str(ready_file)!r}).write_text(str(child.pid), encoding='utf-8'); "
            "time.sleep(15)"
        )

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

    def test_corrupt_task_history_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "codex_task_history.json"
            history.write_text("{broken", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "避免覆盖"):
                CodexRunner("codex", root, 30, projects={})

            self.assertEqual(history.read_text(encoding="utf-8"), "{broken")

    def test_work_command_has_workspace_sandbox(self) -> None:
        with TemporaryDirectory() as directory:
            runner = CodexRunner("codex", Path(directory), 30, projects={})
            args = runner.build_work_args(Path(directory), "任务")
        self.assertIn("workspace-write", args)
        self.assertIn("--json", args)

    def test_work_rejects_a_path_outside_the_configured_project(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            other = root / "other"
            (allowed / ".git").mkdir(parents=True)
            (other / ".git").mkdir(parents=True)
            runner = CodexRunner(
                "codex", root, 30, projects={"demo": allowed}
            )

            ok, response = runner.begin_work("demo", other, "任务", "friend:甲")

            self.assertFalse(ok)
            self.assertIn("允许配置", response)

    def test_history_checkpoint_failure_rolls_back_active_state(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            (project / ".git").mkdir(parents=True)
            runner = CodexRunner(
                "codex", root, 30, projects={"demo": project}
            )
            runner._save_history = lambda _history: (_ for _ in ()).throw(
                OSError("disk")
            )

            with self.assertRaises(OSError):
                runner.begin_work("demo", project, "任务", "friend:甲")

            self.assertFalse(runner.active)
            self.assertEqual(runner._history, [])

    def test_recent_tasks_persist_results_and_are_session_isolated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            (project / ".git").mkdir(parents=True)
            runner = CodexRunner(
                "wechat-codex-command-that-does-not-exist",
                root,
                30,
                projects={"demo": project},
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
            runner = CodexRunner("codex", root, 30, projects={})
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

    def test_continue_is_isolated_by_session_and_survives_restart(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            projects = {
                "project-a": root / "project-a",
                "project-b": root / "project-b",
            }
            for project_path in projects.values():
                (project_path / ".git").mkdir(parents=True)
            runner = CodexRunner("codex", root, 30, projects=projects)
            for session_key, thread_id, project in (
                ("friend:甲", "thread-a", "project-a"),
                ("friend:乙", "thread-b", "project-b"),
            ):
                project_path = projects[project]
                script = (
                    "import json; "
                    f"print(json.dumps({{'type':'thread.started','thread_id':{thread_id!r}}})); "
                    "print(json.dumps({'type':'item.completed','item':"
                    "{'type':'agent_message','text':'done'}}))"
                )
                self.assertTrue(
                    runner._begin(
                        [sys.executable, "-c", script],
                        "work",
                        project,
                        project_path,
                        10,
                        session_key,
                        "test",
                    )[0]
                )
                self.wait_until_idle(runner)

            restarted = CodexRunner("codex", root, 30, projects=projects)
            with patch.object(
                restarted, "_begin", return_value=(True, "started")
            ) as begin:
                self.assertEqual(
                    restarted.begin_continue("继续甲", "friend:甲"),
                    (True, "started"),
                )
                args = begin.call_args.args[0]
                self.assertEqual(args[8], "thread-a")
                self.assertEqual(args[4], "workspace-write")
                self.assertEqual(args[6], str(projects["project-a"]))
                self.assertEqual(args[7], "resume")
                self.assertEqual(begin.call_args.args[2], "project-a")
                self.assertEqual(begin.call_args.args[3], root / "project-a")

            with patch.object(
                restarted, "_begin", return_value=(True, "started")
            ) as begin:
                restarted.begin_continue("继续乙", "friend:乙")
                self.assertEqual(begin.call_args.args[0][8], "thread-b")
                self.assertEqual(begin.call_args.args[2], "project-b")

            ok, _ = restarted.begin_continue("越权继续", "friend:丙")
            self.assertFalse(ok)
            self.assertIn("project-a", restarted.status("friend:甲"))
            self.assertIn("project-b", restarted.status("friend:乙"))
            self.assertNotIn("project-a", restarted.status("friend:丙"))
            self.assertNotIn("project-b", restarted.status("friend:丙"))

            history = json.loads(
                (root / "codex_task_history.json").read_text(encoding="utf-8")
            )
            self.assertEqual(history[0]["thread_id"], "thread-b")
            self.assertEqual(history[0]["project_path"], str(root / "project-b"))

    def test_startup_marks_running_history_as_interrupted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history_file = root / "codex_task_history.json"
            project = root / "demo"
            (project / ".git").mkdir(parents=True)
            history_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "orphaned",
                            "kind": "work",
                            "project": "demo",
                            "project_path": str(project),
                            "session_key": "friend:甲",
                            "thread_id": "thread-orphaned",
                            "prompt": "test",
                            "started_at": 1,
                            "completed_at": None,
                            "status": "运行中",
                            "result": "",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            runner = CodexRunner(
                "codex", root, 30, projects={"demo": project}
            )
            saved = json.loads(history_file.read_text(encoding="utf-8"))

            self.assertEqual(saved[0]["status"], "异常中断")
            self.assertIsInstance(saved[0]["completed_at"], float)
            self.assertIn("程序退出", saved[0]["result"])
            with patch.object(
                runner, "_begin", return_value=(True, "started")
            ) as begin:
                runner.begin_continue("恢复", "friend:甲")
                self.assertEqual(begin.call_args.args[0][8], "thread-orphaned")

    def test_history_resume_target_must_match_current_project_config(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old_project = root / "old-project"
            current_project = root / "current-project"
            (old_project / ".git").mkdir(parents=True)
            (current_project / ".git").mkdir(parents=True)
            (root / "codex_task_history.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "old-task",
                            "kind": "work",
                            "project": "demo",
                            "project_path": str(old_project),
                            "session_key": "friend:甲",
                            "thread_id": "thread-old",
                            "status": "成功",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            runner = CodexRunner(
                "codex",
                root,
                30,
                projects={"demo": current_project},
            )

            ok, response = runner.begin_continue("继续", "friend:甲")
            self.assertFalse(ok)
            self.assertIn("没有可继续", response)

    def test_thread_id_is_persisted_before_a_running_task_finishes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runner = CodexRunner("codex", root, 30, projects={})
            script = (
                "import json, time; "
                "print(json.dumps({'type':'thread.started',"
                "'thread_id':'thread-running'}), flush=True); "
                "time.sleep(15)"
            )
            self.assertTrue(
                runner._begin(
                    [sys.executable, "-c", script],
                    "work",
                    "demo",
                    root,
                    20,
                    "friend:甲",
                    "persist early",
                )[0]
            )
            history_file = root / "codex_task_history.json"
            deadline = time.monotonic() + 5
            thread_id = ""
            try:
                while time.monotonic() < deadline:
                    try:
                        history = json.loads(
                            history_file.read_text(encoding="utf-8")
                        )
                    except (FileNotFoundError, PermissionError):
                        time.sleep(0.01)
                        continue
                    thread_id = str(history[0].get("thread_id") or "")
                    if thread_id:
                        break
                    time.sleep(0.01)

                self.assertTrue(runner.active)
                self.assertEqual(thread_id, "thread-running")
            finally:
                runner.close()
            self.assertFalse(runner.active)

    def test_stop_terminates_descendant_processes_and_waits_for_worker(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready.txt"
            marker = root / "descendant-alive.txt"
            runner = CodexRunner("codex", root, 30, projects={})
            script = self.descendant_process_script(ready, marker, 1.5)
            self.assertTrue(
                runner._begin(
                    [sys.executable, "-c", script],
                    "work",
                    "demo",
                    root,
                    20,
                    "friend:甲",
                    "tree stop",
                )[0]
            )
            self.wait_for_file(ready)

            self.assertEqual(runner.stop(), "正在停止任务")
            self.assertFalse(runner.active)
            time.sleep(1.8)
            self.assertFalse(marker.exists())

    def test_timeout_terminates_descendant_processes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready.txt"
            marker = root / "descendant-alive.txt"
            runner = CodexRunner("codex", root, 30, projects={})
            script = self.descendant_process_script(ready, marker, 2.0)
            self.assertTrue(
                runner._begin(
                    [sys.executable, "-c", script],
                    "work",
                    "demo",
                    root,
                    1,
                    "friend:甲",
                    "tree timeout",
                )[0]
            )
            self.wait_for_file(ready)
            self.wait_until_idle(runner)
            time.sleep(1.3)

            self.assertFalse(marker.exists())
            self.assertIn("[超时]", runner.recent_tasks("friend:甲"))


if __name__ == "__main__":
    unittest.main()
