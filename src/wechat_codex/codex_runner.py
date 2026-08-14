from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


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


@dataclass(frozen=True)
class ParsedOutput:
    final_text: str
    thread_id: str | None
    error_text: str


def parse_jsonl(lines: Iterable[str]) -> ParsedOutput:
    final_messages: list[str] = []
    thread_id: str | None = None
    errors: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = str(event.get("type", ""))
        if event_type == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                final_messages.append(str(item["text"]))
        elif event_type in {"turn.failed", "error"}:
            detail = event.get("error") or event.get("message") or event
            errors.append(str(detail))

    return ParsedOutput(
        final_text=final_messages[-1].strip() if final_messages else "",
        thread_id=thread_id,
        error_text="\n".join(errors).strip(),
    )


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

        self.runtime_dir.mkdir(parents=True, exist_ok=True)

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
            args, "work", project, project_path, self.work_timeout_seconds, session_key
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
            args, "continue", project, project_path, self.work_timeout_seconds, session_key
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
    ) -> tuple[bool, str]:
        with self._lock:
            if self._state.active:
                return False, "已有任务在执行，请发送“状态”或“停止”"
            self._state = RunnerState(
                active=True,
                kind=kind,
                project=project,
                project_path=cwd,
                started_at=time.time(),
                session_key=session_key,
            )

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

            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                self.events.put(RunnerEvent(f"任务超时（{timeout} 秒），已停止", session_key))
                return

            parsed = parse_jsonl(stdout.splitlines())
            with self._lock:
                stopped = self._state.stop_requested
                if parsed.thread_id:
                    self._state.thread_id = parsed.thread_id
                    if self._state.kind in {"work", "continue"}:
                        self._last_work_thread_id = parsed.thread_id
                        self._last_work_project = self._state.project
                        self._last_work_path = self._state.project_path

            if stopped:
                self.events.put(RunnerEvent("任务已停止", session_key))
            elif process.returncode == 0 and parsed.final_text:
                self.events.put(RunnerEvent(parsed.final_text, session_key))
            else:
                detail = parsed.error_text or stderr.strip() or "Codex 没有返回结果"
                self.events.put(RunnerEvent(f"任务失败：{detail[-1200:]}", session_key))
        except FileNotFoundError:
            self.events.put(RunnerEvent(f"找不到 Codex 命令：{self.codex_command}", session_key))
        except Exception as exc:  # pragma: no cover - defensive boundary around subprocesses
            self.events.put(
                RunnerEvent(f"Codex 执行异常：{type(exc).__name__}: {exc}", session_key)
            )
        finally:
            with self._lock:
                self._state.active = False
                self._state.process = None
                self._state.stop_requested = False

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
