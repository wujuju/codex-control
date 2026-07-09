from __future__ import annotations

import signal
import sys
import time
import traceback
from dataclasses import asdict
from typing import NoReturn

from .commands import CommandKind, ParsedCommand, help_text, parse_command
from .config import Config
from .formatting import BRIDGE_MARKER, bridge_comment, fence, read_tail, tail_text, utc_now
from .github_client import GitHubClient, IssueComment
from .runner import CommandRunner, RunResult
from .state import StateStore


class BridgeApp:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = StateStore(cfg.state_path)
        self.github = GitHubClient(cfg.github_token, cfg.github_repo)
        self.runner = CommandRunner(cfg, self.state)
        self.issue_number = cfg.github_issue_number
        self._stopping = False
        self._token_owner = ""
        self.allowed_users: set[str] = set(cfg.github_allowed_users)

    def _select_working_github_auth(self) -> None:
        errors: list[str] = []
        for token, source in self.cfg.github_auth_candidates:
            client = GitHubClient(token, self.cfg.github_repo)
            try:
                owner = client.whoami().lower()
            except Exception as exc:
                errors.append(f"{source}: {exc}")
                continue
            self.github = client
            self._token_owner = owner
            print(f"GitHub auth ok: {source} as @{owner}")
            return
        detail = "\n".join(errors[-5:]) if errors else "no token candidates"
        raise RuntimeError(
            "No valid GitHub authentication token found.\n"
            "Fix one of these options:\n"
            "1. Leave GITHUB_TOKEN blank and run: gh auth login\n"
            "2. Or set GITHUB_TOKEN to a valid token with Issues read/write permission.\n\n"
            f"Tried candidates:\n{detail}"
        )

    def setup(self) -> None:
        self._select_working_github_auth()
        if not self.allowed_users:
            self.allowed_users = {self._token_owner}
        if self.issue_number <= 0:
            self.issue_number = self.github.find_or_create_issue(self.cfg.github_control_issue_title)
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        comments = self.github.list_comments(self.issue_number)
        if self.state.snapshot().last_comment_id == 0 and comments and not self.cfg.catch_up_on_start:
            self.state.update(last_comment_id=max(c.id for c in comments))
        if self.cfg.announce_on_start:
            self.post(
                "Codex GitHub Bridge 已启动。\n\n"
                f"- repo: `{self.cfg.github_repo}`\n"
                f"- auth: `{self.cfg.github_token_source}`\n"
                f"- issue: `#{self.issue_number}`\n"
                f"- local repo: `{self.cfg.repo_path}`\n"
                f"- allowed users: `{', '.join(sorted(self.allowed_users))}`\n\n"
                "发送 `帮助` 查看命令。"
            )

    def run_forever(self) -> NoReturn:
        self.setup()
        print(f"Bridge started for {self.cfg.github_repo} issue #{self.issue_number}")
        print(f"Allowed users: {', '.join(sorted(self.allowed_users))}")
        print(f"Polling every {self.cfg.poll_interval_seconds}s")
        while not self._stopping:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                self._stopping = True
                break
            except Exception:
                traceback.print_exc()
                time.sleep(min(60, self.cfg.poll_interval_seconds))
            time.sleep(self.cfg.poll_interval_seconds)
        print("Bridge stopped")
        sys.exit(0)

    def run_once(self) -> None:
        self.setup()
        self.poll_once()

    def stop(self, *_: object) -> None:
        self._stopping = True
        if self.runner.is_running():
            self.runner.stop()

    def poll_once(self) -> None:
        comments = self.github.list_comments(self.issue_number)
        for comment in comments:
            if self.state.is_processed(comment.id):
                continue
            self.handle_comment(comment)
            self.state.mark_processed(comment.id)

    def handle_comment(self, comment: IssueComment) -> None:
        login = comment.user_login.lower()
        if BRIDGE_MARKER in comment.body:
            return
        if login in self.cfg.github_ignore_users:
            return
        if login not in self.allowed_users:
            parsed = parse_command(comment.body, self.cfg.command_prefix)
            if parsed is not None:
                self.post(
                    f"已忽略来自 `@{comment.user_login}` 的命令。\n\n"
                    f"当前只允许：`{', '.join(sorted(self.allowed_users))}`\n\n"
                    "如果这是你本人，请在 `.env` 设置 `GITHUB_ALLOWED_USERS=你的GitHub用户名`。"
                )
            return
        parsed = parse_command(comment.body, self.cfg.command_prefix)
        if parsed is None:
            return
        self.execute_command(parsed, comment)

    def execute_command(self, cmd: ParsedCommand, comment: IssueComment) -> None:
        if cmd.kind == CommandKind.HELP:
            self.post(help_text(self.cfg.command_prefix))
            return
        if cmd.kind == CommandKind.STATUS:
            self.post(self.status_text())
            return
        if cmd.kind == CommandKind.SUMMARY:
            self.post(self.summary_text())
            return
        if cmd.kind == CommandKind.DIFF:
            diff = self.runner.git_status_and_diff_stat()
            self.post("当前 Git 改动：\n\n" + fence(diff, "text"))
            return
        if cmd.kind == CommandKind.TEST:
            if self.runner.is_running():
                self.post("Codex 正在运行中，先不执行测试。发送 `停止` 可终止当前任务。")
                return
            result = self.runner.run_local_command(self.cfg.test_command, self.cfg.test_timeout_seconds)
            self.post(self.local_result_text("测试", result))
            return
        if cmd.kind == CommandKind.STOP:
            stopped = self.runner.stop()
            self.post("已发送停止信号。" if stopped else "当前没有正在运行的 Codex 任务。")
            return
        if cmd.kind in {CommandKind.CONTINUE, CommandKind.NEW}:
            if not cmd.prompt:
                self.post("命令缺少 prompt。示例：`继续 检查项目是否还有旧接口残留，不要修改代码，先输出分析`")
                return
            if self.runner.is_running():
                self.post("Codex 正在运行中，暂不接受新任务。发送 `状态` 查看，或发送 `停止` 终止。")
                return
            resume = cmd.kind == CommandKind.CONTINUE
            started = self.runner.start_codex_async(
                prompt=cmd.prompt,
                resume=resume,
                comment_id=comment.id,
                on_done=lambda result: self.on_codex_done(result, resume=resume),
            )
            if started:
                self.post(
                    ("已开始继续最近一次 Codex session。" if resume else "已开始新的 Codex 任务。")
                    + "\n\n"
                    + f"触发者：`@{comment.user_login}`\n"
                    + f"Prompt：\n\n{fence(cmd.prompt, 'text')}"
                )
            else:
                self.post("启动失败：已有任务正在运行。")
            return

    def on_codex_done(self, result: RunResult, resume: bool) -> None:
        summary = self.codex_result_text(result, resume=resume)
        self.state.update(last_exit_code=result.exit_code, last_summary=summary, last_codex_log=str(result.log_path))
        try:
            self.post(summary)
        except Exception:
            traceback.print_exc()

    def post(self, body: str) -> None:
        self.github.post_comment(self.issue_number, bridge_comment(body))

    def status_text(self) -> str:
        s = self.state.snapshot()
        return (
            "Codex GitHub Bridge 状态：\n\n"
            f"- bridge: `running`\n"
            f"- codex: `{s.status}`\n"
            f"- pid: `{s.current_pid}`\n"
            f"- repo: `{self.cfg.github_repo}`\n"
            f"- auth: `{self.cfg.github_token_source}`\n"
            f"- issue: `#{self.issue_number}`\n"
            f"- local repo: `{self.cfg.repo_path}`\n"
            f"- last started: `{s.last_started_at or '-'}`\n"
            f"- last finished: `{s.last_finished_at or '-'}`\n"
            f"- last exit code: `{s.last_exit_code}`\n"
            f"- last log: `{s.last_codex_log or '-'}`\n"
            f"- allowed users: `{', '.join(sorted(self.allowed_users))}`"
        )

    def summary_text(self) -> str:
        s = self.state.snapshot()
        if not s.last_summary:
            return "还没有上次执行结果。"
        return s.last_summary

    def local_result_text(self, title: str, result: RunResult) -> str:
        return (
            f"{title}完成。\n\n"
            f"- exit code: `{result.exit_code}`\n"
            f"- elapsed: `{result.elapsed_seconds:.1f}s`\n"
            f"- log: `{result.log_path}`\n\n"
            f"输出：\n\n{fence(tail_text(result.output, 8000), 'text')}"
        )

    def codex_result_text(self, result: RunResult, resume: bool) -> str:
        kind = "继续任务" if resume else "新任务"
        status = "成功" if result.exit_code == 0 else "失败"
        if result.timed_out:
            status = "超时"
        if result.stopped:
            status = "已停止"
        diff = self.runner.git_status_and_diff_stat()
        output_tail = tail_text(result.output or read_tail(result.log_path), 10000)
        return (
            f"Codex {kind}执行完成：**{status}**。\n\n"
            f"- exit code: `{result.exit_code}`\n"
            f"- elapsed: `{result.elapsed_seconds:.1f}s`\n"
            f"- log: `{result.log_path}`\n"
            f"- finished at: `{utc_now()}`\n\n"
            "Git 改动：\n\n"
            f"{fence(diff, 'text')}\n\n"
            "Codex 输出末尾：\n\n"
            f"{fence(output_tail, 'text')}\n\n"
            "下一步可发送：`diff` / `测试` / `继续 <prompt>` / `总结`。"
        )
