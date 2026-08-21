from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from wechat_codex.chat_history import (
    ChatHistoryError,
    ChatHistoryStore,
    ChatMessage,
)


def stored_message(
    message_id: str,
    occurred_at_ms: int,
    *,
    kind: str = "incoming",
    peer_id: str = "user-a",
) -> ChatMessage:
    return ChatMessage(
        message_id=message_id,
        kind=kind,
        sender=peer_id if kind == "incoming" else "微信 Bot",
        peer_id=peer_id,
        text=f"消息 {message_id}",
        occurred_at_ms=occurred_at_ms,
    )


class ChatHistoryStoreTests(unittest.TestCase):
    def test_messages_survive_reopen_and_duplicate_ids_are_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            store = ChatHistoryStore(runtime, "bot-a")
            first = stored_message("first", 1000)
            second = stored_message("second", 2000, kind="outgoing")

            self.assertTrue(store.append(first))
            self.assertTrue(store.append(second))
            self.assertFalse(store.append(second))
            store.close()

            reopened = ChatHistoryStore(runtime, "bot-a")
            self.assertEqual(reopened.latest(20), [first, second])
            self.assertEqual(reopened.count(), 2)
            reopened.close()

    def test_database_is_bound_to_exactly_one_account(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            store = ChatHistoryStore(runtime, "bot-a")
            store.close()

            with self.assertRaisesRegex(ChatHistoryError, "另一个微信账号"):
                ChatHistoryStore(runtime, "bot-b")

    def test_history_pages_are_stable_when_timestamps_match(self) -> None:
        with TemporaryDirectory() as directory:
            store = ChatHistoryStore(Path(directory), "bot-a")
            messages = [stored_message(f"message-{index:03d}", 1000) for index in range(8)]
            for message in messages:
                store.append(message)

            latest = store.latest(3)
            older = store.before(latest[0], 3)

            self.assertEqual(
                [message.message_id for message in latest],
                ["message-005", "message-006", "message-007"],
            )
            self.assertEqual(
                [message.message_id for message in older],
                ["message-002", "message-003", "message-004"],
            )
            store.close()

    def test_clear_only_removes_messages(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            store = ChatHistoryStore(runtime, "bot-a")
            store.append(stored_message("first", 1000))
            store.clear()
            self.assertEqual(store.count(), 0)
            store.close()

            reopened = ChatHistoryStore(runtime, "bot-a")
            self.assertEqual(reopened.latest(10), [])
            reopened.close()


if __name__ == "__main__":
    unittest.main()
