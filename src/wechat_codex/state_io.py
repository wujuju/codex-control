from __future__ import annotations

import logging
import os
from pathlib import Path


log = logging.getLogger(__name__)


def atomic_write_text(path: Path, payload: str, *, mode: int = 0o600) -> None:
    """Atomically replace a UTF-8 state file after flushing its contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            log.debug("清理状态临时文件失败：%s", temporary, exc_info=True)
        raise


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        log.debug("无法打开状态目录进行同步：%s", directory, exc_info=True)
        return
    try:
        os.fsync(descriptor)
    except OSError:
        log.debug("状态目录不支持同步：%s", directory, exc_info=True)
    finally:
        os.close(descriptor)
