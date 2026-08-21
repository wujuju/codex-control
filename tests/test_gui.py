from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from wechat_codex.config import AppConfig
from wechat_codex.chat_history import ChatHistoryStore, ChatMessage
from wechat_codex.gui import (
    BridgeController,
    MessageModel,
    _prepare_gui_logins,
    _prepare_gui_runtime,
)


class BridgeControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QCoreApplication.instance() or QCoreApplication([])

    def test_stop_bridge_runs_blocking_cleanup_off_the_qt_thread(self) -> None:
        config = cast(AppConfig, SimpleNamespace())
        controller = BridgeController(config)
        caller_thread = threading.get_ident()
        stopped = threading.Event()
        stop_threads: list[int] = []

        class FakeBridge:
            def stop(self) -> None:
                stop_threads.append(threading.get_ident())
                stopped.set()

        controller._bridge = FakeBridge()  # type: ignore[assignment]

        controller.stopBridge()

        self.assertTrue(stopped.wait(1))
        self.assertNotEqual(stop_threads, [caller_thread])

    def test_post_event_loop_shutdown_waits_for_async_cleanup(self) -> None:
        config = cast(AppConfig, SimpleNamespace())
        controller = BridgeController(config)
        release = threading.Event()
        stopped = threading.Event()

        class FakeBridge:
            def stop(self) -> None:
                release.wait(1)
                stopped.set()

        controller._bridge = FakeBridge()  # type: ignore[assignment]
        controller.stopBridge()
        timer = threading.Timer(0.02, release.set)
        timer.start()
        self.addCleanup(timer.cancel)

        controller.wait_for_shutdown(timeout_seconds=1)

        self.assertTrue(stopped.is_set())

    def test_bridge_initialization_failure_is_reported_without_starting_thread(self) -> None:
        config = cast(AppConfig, SimpleNamespace())
        controller = BridgeController(config)

        with patch("wechat_codex.gui.BridgeApp", side_effect=RuntimeError("state")):
            controller.startBridge()

        self.assertEqual(controller.statusState, "error")
        self.assertIn("state", controller.statusText)
        self.assertIsNone(controller._thread)

    def test_message_is_rejected_while_bridge_is_stopping(self) -> None:
        config = cast(AppConfig, SimpleNamespace())
        controller = BridgeController(config)
        enqueued: list[str] = []

        class FakeBridge:
            def enqueue_message(self, text: str) -> None:
                enqueued.append(text)

        controller._bridge = FakeBridge()  # type: ignore[assignment]
        controller._state = "stopping"

        controller.sendMessage("不应入队")

        self.assertEqual(enqueued, [])

    def test_account_history_survives_controller_recreation_and_deduplicates(self) -> None:
        with TemporaryDirectory() as directory:
            config = cast(
                AppConfig,
                SimpleNamespace(runtime_dir=Path(directory)),
            )
            message = ChatMessage(
                message_id="incoming:stable",
                kind="incoming",
                sender="用户 A",
                peer_id="user-a",
                text="需要永久保留",
                occurred_at_ms=1_700_000_000_000,
            )
            first = BridgeController(config, account_id="bot-a")
            first._record_event(message)
            first._record_event(message)
            self.qt_app.processEvents()
            self.assertEqual(first.messages.rowCount(), 2)
            first.wait_for_shutdown()

            reopened = BridgeController(config, account_id="bot-a")
            self.assertEqual(reopened.messages.rowCount(), 2)
            index = reopened.messages.index(0, 0)
            self.assertEqual(
                reopened.messages.data(index, MessageModel.TextRole),
                "需要永久保留",
            )
            reopened.wait_for_shutdown()

    def test_clear_messages_deletes_only_current_accounts_database(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first_config = cast(
                AppConfig,
                SimpleNamespace(runtime_dir=root / "account-a"),
            )
            second_config = cast(
                AppConfig,
                SimpleNamespace(runtime_dir=root / "account-b"),
            )
            first = BridgeController(first_config, account_id="bot-a")
            second = BridgeController(second_config, account_id="bot-b")
            first._record_event(
                ChatMessage.create(
                    message_id="a",
                    kind="incoming",
                    sender="A",
                    peer_id="user-a",
                    text="A 的消息",
                )
            )
            second._record_event(
                ChatMessage.create(
                    message_id="b",
                    kind="incoming",
                    sender="B",
                    peer_id="user-b",
                    text="B 的消息",
                )
            )
            self.qt_app.processEvents()

            first.clearMessages()

            self.assertEqual(first.messages.rowCount(), 0)
            self.assertEqual(second._history.count(), 1)  # type: ignore[union-attr]
            first.wait_for_shutdown()
            second.wait_for_shutdown()

    def test_older_history_is_loaded_without_discarding_database_rows(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            store = ChatHistoryStore(runtime, "bot-a")
            for index in range(401):
                store.append(
                    ChatMessage(
                        message_id=f"message-{index:03d}",
                        kind="incoming",
                        sender="A",
                        peer_id="user-a",
                        text=str(index),
                        occurred_at_ms=index,
                    )
                )
            store.close()
            config = cast(AppConfig, SimpleNamespace(runtime_dir=runtime))
            controller = BridgeController(config, account_id="bot-a")

            self.assertTrue(controller.hasOlderMessages)
            self.assertEqual(controller.loadOlderMessages(), 200)
            self.assertTrue(controller.hasOlderMessages)
            self.assertEqual(controller.loadOlderMessages(), 1)
            self.assertFalse(controller.hasOlderMessages)
            self.assertEqual(controller.messages.rowCount(), 402)
            controller.wait_for_shutdown()


class GuiLoginPreparationTests(unittest.TestCase):
    def test_interactive_gui_only_prepares_shared_chatgpt_login(self) -> None:
        config = cast(AppConfig, SimpleNamespace())
        with patch("wechat_codex.gui.ensure_chatgpt_login") as ensure:
            _prepare_gui_logins(
                config, skip_login_check=False, interactive_console=True
            )

        ensure.assert_called_once_with(config)

    def test_start_script_can_skip_duplicate_login_check(self) -> None:
        config = cast(AppConfig, SimpleNamespace())
        with patch("wechat_codex.gui.ensure_chatgpt_login") as ensure:
            _prepare_gui_logins(
                config, skip_login_check=True, interactive_console=False
            )

        ensure.assert_not_called()

    def test_noninteractive_gui_reports_missing_saved_login(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = cast(
                AppConfig,
                SimpleNamespace(
                    ilink_credentials_file=root / "missing-account.json",
                    chatgpt_profile_dir=root / "missing-profile",
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "start.ps1"):
                _prepare_gui_logins(
                    config, skip_login_check=False, interactive_console=False
                )

    def test_runtime_lock_is_acquired_before_login_can_open_a_browser(self) -> None:
        config = cast(AppConfig, SimpleNamespace(runtime_dir=Path(".runtime")))
        calls: list[str] = []

        class FakeLock:
            def release(self) -> None:
                calls.append("release")

        lock = FakeLock()
        with patch(
            "wechat_codex.gui.acquire_instance_lock",
            side_effect=lambda _runtime: calls.append("lock") or lock,
        ), patch(
            "wechat_codex.gui._prepare_gui_logins",
            side_effect=lambda *_args, **_kwargs: calls.append("login"),
        ):
            result = _prepare_gui_runtime(
                config, skip_login_check=False, interactive_console=True
            )

        self.assertIs(result, lock)
        self.assertEqual(calls, ["lock", "login"])

    def test_runtime_lock_is_released_when_gui_login_check_fails(self) -> None:
        config = cast(AppConfig, SimpleNamespace(runtime_dir=Path(".runtime")))
        released: list[bool] = []

        class FakeLock:
            def release(self) -> None:
                released.append(True)

        with patch(
            "wechat_codex.gui.acquire_instance_lock", return_value=FakeLock()
        ), patch(
            "wechat_codex.gui._prepare_gui_logins", side_effect=RuntimeError("login")
        ):
            with self.assertRaisesRegex(RuntimeError, "login"):
                _prepare_gui_runtime(
                    config, skip_login_check=False, interactive_console=True
                )

        self.assertEqual(released, [True])


if __name__ == "__main__":
    unittest.main()
