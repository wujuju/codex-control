from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .formatting import tail_text, utc_now
from .state import StateStore


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int
    output: str
    log_path: Path
    elapsed_seconds: float
    timed_out: bool = False
    stopped: bool = False


class CommandRunner:
    def __init__(self, cfg: Config, state: StateStore) -> None:
        self.cfg = cfg
        self.state = state
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._stop_requested = False

    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def current_pid(self) -> int | None:
        with self._lock:
            if self._process is None:
                return None
            return self._process.pid

    def stop(self) -> bool:
        with self._lock:
            proc = self._process
            self._stop_requested = True
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return True
        finally:
            self.state.update(status="stopping", current_pid=None)

    def start_codex_async(
        self,
        prompt: str,
        resume: bool,
        comment_id: int,
        on_done: Callable[[RunResult], None],
    ) -> bool:
        with self._lock:
            if self.is_running():
                return False
            self._stop_requested = False
        thread = threading.Thread(
            target=self._run_codex_and_callback,
            args=(prompt, resume, comment_id, on_done),
            daemon=True,
        )
        thread.start()
        return True

    def _run_codex_and_callback(
        self,
        prompt: str,
        resume: bool,
        comment_id: int,
        on_done: Callable[[RunResult], None],
    ) -> None:
        result = self.run_codex(prompt=prompt, resume=resume, comment_id=comment_id)
        on_done(result)

    def _codex_args(self, resume: bool) -> list[str]:
        if resume:
            args = [self.cfg.codex_bin, "exec", "resume", "--last", "--cd", str(self.cfg.repo_path)]
        else:
            args = [self.cfg.codex_bin, "exec", "--cd", str(self.cfg.repo_path)]
        args.extend(self.cfg.codex_extra_args)
        args.append("-")
        return args

    def run_codex(self, prompt: str, resume: bool, comment_id: int) -> RunResult:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        safe_kind = "resume" if resume else "new"
        stamp = utc_now().replace(":", "").replace("+", "Z")
        log_path = self.cfg.log_dir / f"codex_{stamp}_{safe_kind}_{comment_id}.log"
        args = self._codex_args(resume=resume)
        started = time.monotonic()
        self.state.update(
            status="running",
            current_pid=None,
            current_comment_id=comment_id,
            last_prompt=prompt,
            last_command=" ".join(args[:-1]) + " -",
            last_started_at=utc_now(),
            last_finished_at="",
            last_exit_code=None,
            last_summary="",
            last_codex_log=str(log_path),
        )

        output_parts: list[str] = []
        timed_out = False
        stopped = False
        proc: subprocess.Popen[str] | None = None
        try:
            env = os.environ.copy()
            proc = subprocess.Popen(
                args,
                cwd=str(self.cfg.repo_path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
            with self._lock:
                self._process = proc
            self.state.update(current_pid=proc.pid)
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()

            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                log.write(f"$ {' '.join(args[:-1])} -\n")
                log.write("--- prompt ---\n")
                log.write(prompt + "\n")
                log.write("--- output ---\n")
                assert proc.stdout is not None
                deadline = None if self.cfg.codex_timeout_seconds <= 0 else time.monotonic() + self.cfg.codex_timeout_seconds
                while True:
                    if deadline is not None and time.monotonic() > deadline:
                        timed_out = True
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        break
                    line = proc.stdout.readline()
                    if line:
                        output_parts.append(line)
                        log.write(line)
                        log.flush()
                        continue
                    if proc.poll() is not None:
                        break
                    time.sleep(0.2)
            exit_code = proc.poll()
            if exit_code is None:
                exit_code = proc.wait(timeout=5)
            with self._lock:
                stopped = self._stop_requested
            output = "".join(output_parts)
            elapsed = time.monotonic() - started
            return RunResult(
                exit_code=int(exit_code),
                output=output,
                log_path=log_path,
                elapsed_seconds=elapsed,
                timed_out=timed_out,
                stopped=stopped,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            msg = f"Runner error: {exc!r}"
            try:
                log_path.write_text(msg, encoding="utf-8", errors="replace")
            except Exception:
                pass
            return RunResult(exit_code=999, output=msg, log_path=log_path, elapsed_seconds=elapsed)
        finally:
            with self._lock:
                self._process = None
                self._stop_requested = False
            self.state.update(
                status="idle",
                current_pid=None,
                current_comment_id=None,
                last_finished_at=utc_now(),
            )

    def run_local_command(self, command: str, timeout_seconds: int) -> RunResult:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().replace(":", "").replace("+", "Z")
        log_path = self.cfg.log_dir / f"local_{stamp}.log"
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.cfg.repo_path),
                shell=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=None if timeout_seconds <= 0 else timeout_seconds,
            )
            output = completed.stdout or ""
            log_path.write_text(output, encoding="utf-8", errors="replace")
            return RunResult(completed.returncode, output, log_path, time.monotonic() - started)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            output += f"\nTimed out after {timeout_seconds}s"
            log_path.write_text(output, encoding="utf-8", errors="replace")
            return RunResult(124, output, log_path, time.monotonic() - started, timed_out=True)
        except Exception as exc:
            output = f"Local command error: {exc!r}"
            log_path.write_text(output, encoding="utf-8", errors="replace")
            return RunResult(999, output, log_path, time.monotonic() - started)

    def git_status_and_diff_stat(self) -> str:
        status = self._git(["status", "--short"])
        diff_stat = self._git(["diff", "--stat"])
        staged_stat = self._git(["diff", "--cached", "--stat"])
        return (
            "# git status --short\n"
            f"{status or '(clean)'}\n\n"
            "# git diff --stat\n"
            f"{diff_stat or '(no unstaged diff)'}\n\n"
            "# git diff --cached --stat\n"
            f"{staged_stat or '(no staged diff)'}"
        )

    def _git(self, args: list[str]) -> str:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(self.cfg.repo_path),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            return completed.stdout.strip()
        except Exception as exc:
            return f"git error: {exc!r}"
