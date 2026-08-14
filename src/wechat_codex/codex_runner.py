from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO


@dataclass(frozen=True)
class RunnerEvent:
    text: str
    session_key: str | None = None


@dataclass
class RunnerState:
    active: bool = False
    kind: str = "idle"
    project: str | None = None
    project_path: Path | None = None
    started_at: float | None = None
    thread_id: str | None = None
    process: subprocess.Popen[str] | None = None
    stop_requested: bool = False
    session_key: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class ParsedOutput:
    final_text: str
    thread_id: str | None
    error_text: str


class _JsonlCollector:
    def __init__(self) -> None:
        self._final_text = ""
        self._thread_id: str | None = None
        self._errors = _TextTail(64 * 1024)
        self._lock = threading.Lock()

    def feed(self, raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return

        event_type = str(event.get("type", ""))
        with self._lock:
            if event_type == "thread.started":
                self._thread_id = str(event.get("thread_id") or self._thread_id or "") or None
            elif event_type == "item.completed":
                item = event.get("item") or {}
                if (
                    isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and item.get("text")
                ):
                    self._final_text = str(item["text"]).strip()
            elif event_type in {"turn.failed", "error"}:
                detail = event.get("error") or event.get("message") or event
                self._errors.append(str(detail) + "\n")

    def result(self) -> ParsedOutput:
        with self._lock:
            return ParsedOutput(
                final_text=self._final_text,
                thread_id=self._thread_id,
                error_text=self._errors.text().strip(),
            )


class _TextTail:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._parts: deque[str] = deque()
        self._size = 0

    def append(self, value: str) -> None:
        if not value:
            return
        value = value[-self.limit :]
        self._parts.append(value)
        self._size += len(value)
        while self._size > self.limit and self._parts:
            excess = self._size - self.limit
            first = self._parts[0]
            if len(first) <= excess:
                self._parts.popleft()
                self._size -= len(first)
            else:
                self._parts[0] = first[excess:]
                self._size -= excess

    def text(self) -> str:
        return "".join(self._parts)


def _read_jsonl_stream(stream: TextIO, collector: _JsonlCollector) -> None:
    with stream:
        for line in stream:
            collector.feed(line)


def _read_text_tail(stream: TextIO, tail: _TextTail) -> None:
    with stream:
        for line in stream:
            tail.append(line)


def parse_jsonl(lines: Iterable[str]) -> ParsedOutput:
    collector = _JsonlCollector()
    for line in lines:
        collector.feed(line)

    return collector.result()


class CodexRunner:
    def __init__(
        self,
        codex_command: str,
        runtime_dir: Path,
        work_timeout_seconds: int,
    ) -> None:
        self.codex_command = codex_command
        self.runtime_dir = runtime_dir
        self.work_timeout_seconds = work_timeout_seconds
        self.events: queue.Queue[RunnerEvent] = queue.Queue()
        self._state = RunnerState()
        self._last_work_thread_id: str | None = None
        self._last_work_project: str | None = None
        self._last_work_path: Path | None = None
        self._lock = threading.RLock()
        self._history_file = runtime_dir / "codex_task_history.json"

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._history = self._load_history()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state.active

    def begin_work(
        self,
        project: str,
        project_path: Path,
        prompt: str,
        session_key: str | None = None,
    ) -> tuple[bool, str]:
        if not project_path.is_dir():
            return False, f"项目目录不存在：{project_path}"
        if not (project_path / ".git").exists():
            return False, f"项目不是 Git 仓库：{project_path}"

        work_prompt = (
            "你正在通过微信接受本机开发任务。请在当前项目内完成任务并进行必要验证。"
            "不要提交或推送 Git，不要部署，不要修改项目目录之外的文件，不要执行破坏性操作。"
            "最终用中文简洁说明改了什么、验证结果和未解决问题。\n\n任务：\n" + prompt
        )
        args = self.build_work_args(project_path, work_prompt)
        return self._begin(
            args,
            "work",
            project,
            project_path,
            self.work_timeout_seconds,
            session_key,
            prompt,
        )

    def begin_continue(
        self, prompt: str, session_key: str | None = None
    ) -> tuple[bool, str]:
        with self._lock:
            thread_id = self._last_work_thread_id
            project = self._last_work_project
            project_path = self._last_work_path
        if not thread_id or not project or not project_path:
            return False, "当前程序运行期间还没有可继续的干活会话"
        continue_prompt = (
            "继续处理微信用户的新要求。仍然不要提交、推送或部署。完成后用中文简洁汇报。\n\n新要求：\n"
            + prompt
        )
        args = self.build_continue_args(thread_id, continue_prompt)
        return self._begin(
            args,
            "continue",
            project,
            project_path,
            self.work_timeout_seconds,
            session_key,
            prompt,
        )

    def build_work_args(self, project_path: Path, prompt: str) -> list[str]:
        return [
            self.codex_command,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "-C",
            str(project_path),
            prompt,
        ]

    def build_continue_args(self, thread_id: str, prompt: str) -> list[str]:
        return [self.codex_command, "exec", "resume", "--json", thread_id, prompt]

    def _begin(
        self,
        args: list[str],
        kind: str,
        project: str | None,
        cwd: Path,
        timeout: int,
        session_key: str | None,
        description: str,
    ) -> tuple[bool, str]:
        task_id = f"task-{time.time_ns()}"
        started_at = time.time()
        with self._lock:
            if self._state.active:
                return False, "已有任务在执行，请发送 @状态 或 @停止"
            self._state = RunnerState(
                active=True,
                kind=kind,
                project=project,
                project_path=cwd,
                started_at=started_at,
                session_key=session_key,
                task_id=task_id,
            )
            self._history.insert(0, {
                "id": task_id,
                "kind": kind,
                "project": project or "",
                "session_key": session_key or "",
                "prompt": description.strip()[:1000],
                "started_at": started_at,
                "completed_at": None,
                "status": "运行中",
                "result": "",
            })
            self._history = self._history[:50]
            history = [dict(record) for record in self._history]
        self._save_history(history)

        worker = threading.Thread(
            target=self._run,
            args=(args, cwd, timeout),
            name=f"codex-{kind}",
            daemon=True,
        )
        worker.start()
        return True, f"已开始 Codex 任务（{project}）"

    def _run(self, args: list[str], cwd: Path, timeout: int) -> None:
        with self._lock:
            session_key = self._state.session_key
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            process = subprocess.Popen(
                args,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=flags,
            )
            with self._lock:
                self._state.process = process
                stop_immediately = self._state.stop_requested
            if stop_immediately:
                process.terminate()

            assert process.stdout is not None
            assert process.stderr is not None
            collector = _JsonlCollector()
            stderr_tail = _TextTail(64 * 1024)
            stdout_reader = threading.Thread(
                target=_read_jsonl_stream,
                args=(process.stdout, collector),
                name="codex-stdout-reader",
                daemon=True,
            )
            stderr_reader = threading.Thread(
                target=_read_text_tail,
                args=(process.stderr, stderr_tail),
                name="codex-stderr-reader",
                daemon=True,
            )
            stdout_reader.start()
            stderr_reader.start()
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            stdout_reader.join(timeout=2)
            stderr_reader.join(timeout=2)
            if timed_out:
                result = f"任务超时（{timeout} 秒），已停止"
                self._complete_task("超时", result)
                self.events.put(RunnerEvent(result, session_key))
                return

            parsed = collector.result()
            with self._lock:
                stopped = self._state.stop_requested
                if parsed.thread_id:
                    self._state.thread_id = parsed.thread_id
                    if self._state.kind in {"work", "continue"}:
                        self._last_work_thread_id = parsed.thread_id
                        self._last_work_project = self._state.project
                        self._last_work_path = self._state.project_path

            if stopped:
                result = "任务已停止"
                self._complete_task("已停止", result)
                self.events.put(RunnerEvent(result, session_key))
            elif process.returncode == 0 and parsed.final_text:
                self._complete_task("成功", parsed.final_text)
                self.events.put(RunnerEvent(parsed.final_text, session_key))
            else:
                detail = parsed.error_text or stderr_tail.text().strip() or "Codex 没有返回结果"
                result = f"任务失败：{detail[-1200:]}"
                self._complete_task("失败", result)
                self.events.put(RunnerEvent(result, session_key))
        except FileNotFoundError:
            result = f"找不到 Codex 命令：{self.codex_command}"
            self._complete_task("失败", result)
            self.events.put(RunnerEvent(result, session_key))
        except Exception as exc:  # pragma: no cover - defensive boundary around subprocesses
            result = f"Codex 执行异常：{type(exc).__name__}: {exc}"
            self._complete_task("失败", result)
            self.events.put(
                RunnerEvent(result, session_key)
            )
        finally:
            with self._lock:
                self._state.active = False
                self._state.process = None
                self._state.stop_requested = False

    def _load_history(self) -> list[dict[str, object]]:
        if not self._history_file.is_file():
            return []
        try:
            raw = json.loads(self._history_file.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("顶层不是 JSON 数组")
            return [dict(record) for record in raw if isinstance(record, dict)][:50]
        except Exception:
            return []

    def _save_history(self, history: list[dict[str, object]]) -> None:
        temporary = self._history_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(history, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._history_file)

    def _complete_task(self, status: str, result: str) -> None:
        with self._lock:
            task_id = self._state.task_id
            for record in self._history:
                if str(record.get("id") or "") == task_id:
                    record["status"] = status
                    record["result"] = result.strip()[:4000]
                    record["completed_at"] = time.time()
                    break
            history = [dict(record) for record in self._history]
        self._save_history(history)

    def recent_tasks(
        self,
        session_key: str | None = None,
        limit: int = 10,
    ) -> str:
        with self._lock:
            records = [
                dict(record)
                for record in self._history
                if session_key is None
                or str(record.get("session_key") or "") == session_key
            ][:limit]
        if not records:
            return "当前没有保存的 Codex 任务"
        lines = ["最近 Codex 任务："]
        for index, record in enumerate(records, start=1):
            started_value = record.get("started_at")
            started_at = (
                float(started_value)
                if isinstance(started_value, (int, float))
                else 0.0
            )
            started = (
                time.strftime("%m-%d %H:%M", time.localtime(started_at))
                if started_at
                else "时间未知"
            )
            project = str(record.get("project") or "未知项目")
            status = str(record.get("status") or "未知")
            prompt = " ".join(str(record.get("prompt") or "").split())[:120]
            result = " ".join(str(record.get("result") or "").split())[:180]
            lines.append(f"{index}. {started} [{status}] {project}：{prompt or '无描述'}")
            if result:
                lines.append(f"   结果：{result}")
        return "\n".join(lines)

    def stop(self) -> str:
        with self._lock:
            if not self._state.active:
                return "当前没有执行中的任务"
            self._state.stop_requested = True
            process = self._state.process
        if process and process.poll() is None:
            process.terminate()
        return "正在停止任务"

    def status(self) -> str:
        with self._lock:
            state = self._state
            if not state.active:
                if self._last_work_thread_id:
                    return f"当前空闲；最近干活项目：{self._last_work_project}"
                return "当前空闲"
            elapsed = int(time.time() - (state.started_at or time.time()))
            project = f"，项目：{state.project}" if state.project else ""
            return f"正在执行 {state.kind}{project}，已运行 {elapsed} 秒"

    def drain_events(self) -> list[RunnerEvent]:
        result: list[RunnerEvent] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result
