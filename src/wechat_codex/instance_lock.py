from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import errno
import importlib
import logging
import os
from pathlib import Path
import threading
from typing import BinaryIO


log = logging.getLogger(__name__)

INSTANCE_LOCK_FILENAME = "wechat-codex.lock"


class AlreadyRunningError(RuntimeError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            "已有微信 Codex 实例正在运行，请先停止现有实例再重试"
            f"（运行锁：{path}）"
        )


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = importlib.import_module("msvcrt")
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl = importlib.import_module("fcntl")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = importlib.import_module("msvcrt")
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl = importlib.import_module("fcntl")
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_lock_contention(error: OSError) -> bool:
    return error.errno in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLK,
        errno.EPERM,
    } or getattr(error, "winerror", None) in {32, 33}


class InstanceLock:
    """A non-blocking process lock backed by a persistent runtime file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._handle: BinaryIO | None = None
        self._guard = threading.RLock()

    @property
    def acquired(self) -> bool:
        with self._guard:
            return self._handle is not None

    def acquire(self) -> InstanceLock:
        with self._guard:
            if self._handle is not None:
                return self
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            locked = False
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                _lock_file(handle)
                locked = True
                handle.seek(1)
                handle.truncate()
                handle.write(f"pid={os.getpid()}\n".encode("ascii"))
                handle.flush()
            except OSError as exc:
                if locked:
                    try:
                        _unlock_file(handle)
                    except OSError:
                        pass
                handle.close()
                if _is_lock_contention(exc):
                    raise AlreadyRunningError(self.path) from None
                raise
            self._handle = handle
            return self

    def release(self) -> None:
        with self._guard:
            handle = self._handle
            self._handle = None
            if handle is None:
                return
            try:
                _unlock_file(handle)
            except OSError:
                # Closing the descriptor also releases the OS lock. Keep
                # shutdown best-effort without masking an application error.
                log.warning("显式释放运行锁失败，将通过关闭文件释放", exc_info=True)
            finally:
                handle.close()

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()


def acquire_instance_lock(runtime_dir: str | Path) -> InstanceLock:
    return InstanceLock(Path(runtime_dir) / INSTANCE_LOCK_FILENAME).acquire()


@contextmanager
def runtime_instance_lock(runtime_dir: str | Path) -> Iterator[InstanceLock]:
    lock = acquire_instance_lock(runtime_dir)
    try:
        yield lock
    finally:
        lock.release()
