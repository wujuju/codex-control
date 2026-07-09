from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


_PLACEHOLDER_FRAGMENTS = (
    "你的",
    "your",
    "example",
    "xxxxxxxx",
    "ghp_xxx",
    "githubtoken",
    "github_token",
    "replace_me",
)


def _clean_token(raw: str | None) -> str:
    token = (raw or "").strip().strip('"').strip("'")
    if not token:
        return ""
    lowered = token.lower()
    if any(fragment in lowered for fragment in _PLACEHOLDER_FRAGMENTS):
        return ""
    return token


def _github_token_from_gh_cli() -> str:
    """Return the active GitHub CLI token, or an empty string if unavailable."""
    if shutil.which("gh") is None:
        return ""
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return _clean_token(proc.stdout)


def _github_auth_candidates() -> tuple[tuple[str, str], ...]:
    """Return auth candidates in priority order.

    Older versions failed fast when GITHUB_TOKEN existed but was stale. This version
    validates the token at startup and can fall back to GH_TOKEN or GitHub CLI.
    """
    candidates: list[tuple[str, str]] = []
    for source, token in (
        ("GITHUB_TOKEN", _clean_token(os.getenv("GITHUB_TOKEN"))),
        ("GH_TOKEN", _clean_token(os.getenv("GH_TOKEN"))),
        ("gh auth token", _github_token_from_gh_cli()),
    ):
        if not token:
            continue
        if any(token == existing for existing, _ in candidates):
            continue
        candidates.append((token, source))
    return tuple(candidates)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _csv(name: str) -> set[str]:
    raw = os.getenv(name, "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _path(raw: str, base_dir: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


@dataclass(frozen=True, slots=True)
class Config:
    base_dir: Path
    github_repo: str
    github_issue_number: int
    github_control_issue_title: str
    github_token: str
    github_token_source: str
    github_auth_candidates: tuple[tuple[str, str], ...]
    github_allowed_users: set[str]
    github_ignore_users: set[str]
    poll_interval_seconds: int
    catch_up_on_start: bool
    command_prefix: str
    repo_path: Path
    codex_bin: str
    codex_extra_args: list[str]
    codex_timeout_seconds: int
    test_command: str
    test_timeout_seconds: int
    state_path: Path
    log_dir: Path
    announce_on_start: bool

    @property
    def owner(self) -> str:
        return self.github_repo.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.github_repo.split("/", 1)[1]

    def validate(self) -> None:
        if "/" not in self.github_repo:
            raise ValueError("GITHUB_REPO must look like owner/repo")
        if not self.github_auth_candidates:
            raise ValueError(
                "GitHub authentication is required. Either set a valid GITHUB_TOKEN/GH_TOKEN, "
                "or install GitHub CLI and run: gh auth login"
            )
        if not self.repo_path.exists():
            raise ValueError(f"REPO_PATH does not exist: {self.repo_path}")
        if self.poll_interval_seconds < 5:
            raise ValueError("POLL_INTERVAL_SECONDS should be >= 5")


def load_config() -> Config:
    base_dir = Path(__file__).resolve().parents[1]
    load_dotenv(base_dir / ".env")

    import shlex

    candidates = _github_auth_candidates()
    token, token_source = candidates[0] if candidates else ("", "none")

    cfg = Config(
        base_dir=base_dir,
        github_repo=os.getenv("GITHUB_REPO", "wujuju/codex-control").strip(),
        github_issue_number=_int("GITHUB_ISSUE_NUMBER", 0),
        github_control_issue_title=os.getenv("GITHUB_CONTROL_ISSUE_TITLE", "Codex Control Console").strip(),
        github_token=token,
        github_token_source=token_source,
        github_auth_candidates=candidates,
        github_allowed_users=_csv("GITHUB_ALLOWED_USERS"),
        github_ignore_users=_csv("GITHUB_IGNORE_USERS"),
        poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 15),
        catch_up_on_start=_bool("CATCH_UP_ON_START", False),
        command_prefix=os.getenv("COMMAND_PREFIX", "").strip(),
        repo_path=Path(os.getenv("REPO_PATH", r"D:\Sam\HGameAI")).expanduser(),
        codex_bin=os.getenv("CODEX_BIN", "codex").strip(),
        codex_extra_args=shlex.split(os.getenv("CODEX_EXTRA_ARGS", ""), posix=os.name != "nt"),
        codex_timeout_seconds=_int("CODEX_TIMEOUT_SECONDS", 0),
        test_command=os.getenv("TEST_COMMAND", "python -m pytest -q").strip(),
        test_timeout_seconds=_int("TEST_TIMEOUT_SECONDS", 1200),
        state_path=_path(os.getenv("STATE_PATH", ".codex_github_bridge/state.json"), base_dir),
        log_dir=_path(os.getenv("LOG_DIR", ".codex_github_bridge/logs"), base_dir),
        announce_on_start=_bool("ANNOUNCE_ON_START", False),
    )
    cfg.validate()
    return cfg
