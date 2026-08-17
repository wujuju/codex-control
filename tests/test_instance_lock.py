from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from wechat_codex.instance_lock import AlreadyRunningError, InstanceLock


class InstanceLockTests(unittest.TestCase):
    def test_second_instance_is_rejected_and_can_start_after_release(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "wechat-codex.lock"
            first = InstanceLock(path)
            second = InstanceLock(path)
            first.acquire()

            with self.assertRaisesRegex(AlreadyRunningError, "已有微信 Codex"):
                second.acquire()

            first.release()
            second.acquire()
            second.release()

    def test_context_manager_releases_lock_when_body_raises(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "wechat-codex.lock"

            with self.assertRaisesRegex(RuntimeError, "boom"):
                with InstanceLock(path):
                    raise RuntimeError("boom")

            with InstanceLock(path):
                pass

    def test_acquire_and_release_are_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            lock = InstanceLock(Path(directory) / "wechat-codex.lock")

            self.assertIs(lock.acquire(), lock)
            self.assertIs(lock.acquire(), lock)
            lock.release()
            lock.release()


if __name__ == "__main__":
    unittest.main()
