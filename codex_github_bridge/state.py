from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class BridgeState:
    last_comment_id: int = 0
    status: str = "idle"
    current_pid: int | None = None
    current_comment_id: int | None = None
    last_prompt: str = ""
    last_command: str = ""
    last_started_at: str = ""
    last_finished_at: str = ""
    last_exit_code: int | None = None
    last_summary: str = ""
    last_codex_log: str = ""
    processed_comment_ids: list[int] = field(default_factory=list)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> BridgeState:
        if not self.path.exists():
            return BridgeState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            allowed = {field.name for field in BridgeState.__dataclass_fields__.values()}
            clean = {k: v for k, v in data.items() if k in allowed}
            return BridgeState(**clean)
        except Exception:
            return BridgeState()

    def snapshot(self) -> BridgeState:
        with self._lock:
            data = asdict(self._state)
            return BridgeState(**data)

    def update(self, **kwargs: Any) -> BridgeState:
        with self._lock:
            for key, value in kwargs.items():
                if not hasattr(self._state, key):
                    raise KeyError(key)
                setattr(self._state, key, value)
            self.save_locked()
            return self.snapshot()

    def mark_processed(self, comment_id: int) -> None:
        with self._lock:
            self._state.last_comment_id = max(self._state.last_comment_id, comment_id)
            if comment_id not in self._state.processed_comment_ids:
                self._state.processed_comment_ids.append(comment_id)
                self._state.processed_comment_ids = self._state.processed_comment_ids[-200:]
            self.save_locked()

    def is_processed(self, comment_id: int) -> bool:
        with self._lock:
            return comment_id <= self._state.last_comment_id or comment_id in self._state.processed_comment_ids

    def save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self._state), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
