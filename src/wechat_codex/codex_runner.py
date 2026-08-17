from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TextIO

from .state_io import atomic_write_text


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
class _ResumeTarget:
    thread_id: str
    project: str
    project_path: Path


@dataclass(frozen=True)
class ParsedOutput:
    final_text: str
    thread_id: str | None
    error_text: str


class _JsonlCollector:
    def __init__(
        self,
        on_thread_started: Callable[[str], None] | None = None,
    ) -> None:
        self._final_text = ""
        self._thread_id: str | None = None
        self._errors = _TextTail(64 * 1024)
        self._lock = threading.Lock()
        self._on_thread_started = on_thread_started

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
        started_thread_id: str | None = None
        with self._lock:
            if event_type == "thread.started":
                thread_id = str(event.get("thread_id") or self._thread_id or "") or None
                if thread_id != self._thread_id:
                    started_thread_id = thread_id
                self._thread_id = thread_id
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
        if started_thread_id and self._on_thread_started is not None:
            try:
                self._on_thread_started(started_thread_id)
            except OSError:
                pass

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
        *,
        projects: dict[str, Path],
    ) -> None:
        self.codex_command = codex_command
        self.runtime_dir = runtime_dir
        self.work_timeout_seconds = work_timeout_seconds
        self._projects = {
            str(name): path.expanduser().resolve()
            for name, path in projects.items()
            if str(name)
        }
        self.events: queue.Queue[RunnerEvent] = queue.Queue()
        self._state = RunnerState()
        self._resume_targets: dict[str, _ResumeTarget] = {}
        self._lock = threading.RLock()
        self._termination_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._history_file = runtime_dir / "codex_task_history.json"

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._history = self._load_history()
        self._initialize_history()

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
        configured_path = self._allowed_project_path(project, project_path)
        if configured_path is None:
            return False, f"项目不在当前允许配置中：{project}"
        project_path = configured_path
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
            key = self._resume_key(session_key)
            target = self._resume_targets.get(key)
        if target is None:
            return False, "当前会话还没有可继续的干活会话"
        project_path = self._allowed_project_path(
            target.project,
            target.project_path,
        )
        if (
            project_path is None
            or not project_path.is_dir()
            or not (project_path / ".git").exists()
        ):
            with self._lock:
                self._resume_targets.pop(key, None)
            return False, "原 Codex 项目已从配置移除或不再是有效 Git 仓库，不能继续"
        continue_prompt = (
            "继续处理微信用户的新要求。仍然不要提交、推送或部署。完成后用中文简洁汇报。\n\n新要求：\n"
            + prompt
        )
        args = self.build_continue_args(
            target.thread_id,
            continue_prompt,
            project_path,
        )
        return self._begin(
            args,
            "continue",
            target.project,
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

    def build_continue_args(
        self,
        thread_id: str,
        prompt: str,
        project_path: Path,
    ) -> list[str]:
        return [
            self.codex_command,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "-C",
            str(project_path),
            "resume",
            thread_id,
            prompt,
        ]

    def _allowed_project_path(
        self,
        project: str,
        project_path: Path,
    ) -> Path | None:
        configured = self._projects.get(project)
        if configured is None:
            return None
        try:
            candidate = project_path.expanduser().resolve()
        except OSError:
            return None
        return configured if candidate == configured else None

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
                "project_path": str(cwd),
                "session_key": session_key or "",
                "thread_id": "",
                "prompt": description.strip()[:1000],
                "started_at": started_at,
                "completed_at": None,
                "status": "运行中",
                "result": "",
            })
            self._history = self._history[:50]
            history = [dict(record) for record in self._history]
        try:
            self._save_history(history)
        except Exception:
            with self._lock:
                if self._state.task_id == task_id:
                    self._state = RunnerState()
                self._history = [
                    record
                    for record in self._history
                    if str(record.get("id") or "") != task_id
                ]
            raise

        worker = threading.Thread(
            target=self._run,
            args=(args, cwd, timeout),
            name=f"codex-{kind}",
            daemon=True,
        )
        with self._lock:
            self._worker = worker
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
                start_new_session=os.name != "nt",
            )
            with self._lock:
                self._state.process = process
                stop_immediately = self._state.stop_requested
            if stop_immediately:
                self._terminate_process_tree(process)

            assert process.stdout is not None
            assert process.stderr is not None
            collector = _JsonlCollector(self._remember_work_thread)
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
                self._terminate_process_tree(process)
            stdout_reader.join(timeout=2)
            stderr_reader.join(timeout=2)
            parsed = collector.result()
            if parsed.thread_id:
                self._remember_work_thread(parsed.thread_id)
            if timed_out:
                result = f"任务超时（{timeout} 秒），已停止"
                self._complete_task("超时", result)
                self.events.put(RunnerEvent(result, session_key))
                return

            with self._lock:
                stopped = self._state.stop_requested

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

    @staticmethod
    def _resume_key(session_key: str | None) -> str:
        return session_key or ""

    def _remember_work_thread(self, thread_id: str) -> None:
        with self._lock:
            state = self._state
            if (
                state.kind not in {"work", "continue"}
                or not state.project
                or state.project_path is None
            ):
                return
            state.thread_id = thread_id
            target = _ResumeTarget(thread_id, state.project, state.project_path)
            self._resume_targets[self._resume_key(state.session_key)] = target
            for record in self._history:
                if str(record.get("id") or "") == state.task_id:
                    record["thread_id"] = thread_id
                    record["project"] = state.project
                    record["project_path"] = str(state.project_path)
                    break
            history = [dict(record) for record in self._history]
        self._save_history(history)

    def _initialize_history(self) -> None:
        changed = False
        interrupted_at = time.time()
        for record in self._history:
            if str(record.get("status") or "") not in {"运行中", "running"}:
                continue
            record["status"] = "异常中断"
            record["completed_at"] = interrupted_at
            record["result"] = "程序退出前任务未完成，已标记为异常中断"
            changed = True
        if changed:
            self._save_history([dict(record) for record in self._history])

        for record in self._history:
            if str(record.get("kind") or "") not in {"work", "continue"}:
                continue
            thread_id = str(record.get("thread_id") or "")
            project = str(record.get("project") or "")
            project_path = str(record.get("project_path") or "")
            if not thread_id or not project or not project_path:
                continue
            session_key = self._resume_key(str(record.get("session_key") or ""))
            if session_key in self._resume_targets:
                continue
            allowed_path = self._allowed_project_path(project, Path(project_path))
            if (
                allowed_path is None
                or not allowed_path.is_dir()
                or not (allowed_path / ".git").exists()
            ):
                continue
            target = _ResumeTarget(thread_id, project, allowed_path)
            self._resume_targets[session_key] = target

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
        with self._termination_lock:
            if os.name == "nt":
                if process.poll() is not None:
                    return
                try:
                    subprocess.run(
                        [
                            "taskkill.exe",
                            "/PID",
                            str(process.pid),
                            "/T",
                            "/F",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except (OSError, subprocess.SubprocessError):
                    if process.poll() is None:
                        process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                return

            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            except OSError:
                if process.poll() is None:
                    process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                if process.poll() is None:
                    process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    process.kill()

    def _load_history(self) -> list[dict[str, object]]:
        if not self._history_file.is_file():
            return []
        try:
            raw = json.loads(self._history_file.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("顶层不是 JSON 数组")
            if any(not isinstance(record, dict) for record in raw):
                raise ValueError("任务记录包含非 JSON 对象")
            return [dict(record) for record in raw][:50]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Codex 任务历史无法读取，已停止以避免覆盖 {self._history_file}: {exc}"
            ) from exc

    def _save_history(self, history: list[dict[str, object]]) -> None:
        atomic_write_text(
            self._history_file,
            json.dumps(history, ensure_ascii=False, indent=2) + "\n",
        )

    def _complete_task(self, status: str, result: str) -> None:
        with self._lock:
            task_id = self._state.task_id
            for record in self._history:
                if str(record.get("id") or "") == task_id:
                    record["status"] = status
                    record["result"] = result.strip()[:4000]
                    record["completed_at"] = time.time()
                    record["thread_id"] = self._state.thread_id or ""
                    if self._state.project_path is not None:
                        record["project_path"] = str(self._state.project_path)
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
            was_active = self._state.active
            if was_active:
                self._state.stop_requested = True
            process = self._state.process
            worker = self._worker
        if was_active and process and process.poll() is None:
            self._terminate_process_tree(process)
        if worker is not None and worker is not threading.current_thread():
            if worker.is_alive():
                worker.join(timeout=8)
            if worker.is_alive():
                with self._lock:
                    process = self._state.process
                if process and process.poll() is None:
                    self._terminate_process_tree(process)
                worker.join(timeout=2)
        return "正在停止任务" if was_active else "当前没有执行中的任务"

    def close(self) -> None:
        self.stop()
        with self._lock:
            worker = self._worker
        if (
            worker is not None
            and worker is not threading.current_thread()
            and worker.is_alive()
        ):
            worker.join(timeout=2)

    def status(self, session_key: str | None = None) -> str:
        with self._lock:
            state = self._state
            if not state.active:
                target = self._resume_targets.get(self._resume_key(session_key))
                if target is not None:
                    return f"当前空闲；最近干活项目：{target.project}"
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
