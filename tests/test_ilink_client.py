import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from wechat_codex.ilink_api import ILinkProtocolError
from wechat_codex.ilink_client import ILinkClient, ReplyTarget, split_text


class FakeAPI:
    def __init__(self) -> None:
        self.messages = []

    def send_message(self, message):
        self.messages.append(message)

    def download_image(self, _image_item):
        return b"\x89PNG\r\n\x1a\nimage-data"

    def upload_image(self, payload, to_user_id):
        return {
            "media": {"encrypt_query_param": f"cdn-{to_user_id}"},
            "mid_size": len(payload),
        }

    def upload_file(self, payload, to_user_id, file_name):
        return {
            "media": {"encrypt_query_param": f"file-{to_user_id}"},
            "file_name": file_name,
            "len": str(len(payload)),
        }


class PollAPI:
    def __init__(self) -> None:
        self.calls = 0

    def get_updates(self, _cursor, _timeout):
        self.calls += 1
        return {"get_updates_buf": f"cursor-{self.calls}", "msgs": []}


class FastEvent:
    def __init__(self, api: PollAPI) -> None:
        self.api = api

    def is_set(self) -> bool:
        return self.api.calls >= 6

    def wait(self, _timeout=None) -> bool:
        return False


class ILinkClientTests(unittest.TestCase):
    def test_existing_corrupt_state_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            state_path.write_text("{broken", encoding="utf-8")

            with self.assertRaisesRegex(ILinkProtocolError, "避免重复处理消息"):
                self.make_client(root)

            self.assertEqual(state_path.read_text(encoding="utf-8"), "{broken")

            state_path.write_text(
                json.dumps({"pending": [{}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ILinkProtocolError, "key/content/sender"):
                self.make_client(root)

    def test_image_message_is_persisted_and_materialized(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            client = self.make_client(root)
            client._api = FakeAPI()
            client._record_response({
                "get_updates_buf": "next",
                "msgs": [{
                    "message_type": 1,
                    "message_id": 9,
                    "from_user_id": "user-a",
                    "context_token": "ctx-image",
                    "item_list": [{
                        "type": 2,
                        "image_item": {
                            "media": {
                                "encrypt_query_param": "encrypted",
                                "aes_key": "key",
                            }
                        },
                    }],
                }],
            })

            incoming = client.poll()[0]
            self.assertEqual(incoming.content, "[图片]")
            self.assertIsNotNone(incoming.image_item)

            materialized = client.materialize_image(incoming)

            image_path = Path(materialized.image_path)
            self.assertTrue(image_path.is_file())
            self.assertEqual(image_path.suffix, ".png")
            self.assertEqual(image_path.read_bytes(), b"\x89PNG\r\n\x1a\nimage-data")
            self.assertEqual(
                client._state["pending"][0]["image_path"], str(image_path.resolve())
            )

    def make_client(self, root: Path) -> ILinkClient:
        client = ILinkClient(
            root / "account.json",
            root / "state.json",
            response_prefix="[Bot] ",
            max_reply_chars=100,
        )
        client._credentials = SimpleNamespace(account_id="bot-1", user_id="owner")
        return client

    def test_normalizes_text_and_voice_transcript_but_ignores_bot_message(self) -> None:
        with TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            incoming = client._normalize({
                "message_type": 1,
                "message_id": 7,
                "from_user_id": "user-a",
                "context_token": "ctx",
                "item_list": [
                    {"type": 1, "text_item": {"text": "第一行"}},
                    {"type": 3, "voice_item": {"text": "语音内容"}},
                ],
            })

            self.assertIsNotNone(incoming)
            self.assertEqual(incoming.content, "第一行\n语音内容")
            self.assertEqual(incoming.reply_target, ReplyTarget("user-a", "ctx"))
            self.assertIsNone(client._normalize({
                "message_type": 2,
                "from_user_id": "user-a",
                "item_list": [{"type": 1, "text_item": {"text": "机器人回声"}}],
            }))

    def test_cursor_pending_message_and_ack_are_persisted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            client = self.make_client(root)
            client._record_response({
                "get_updates_buf": "next",
                "msgs": [{
                    "message_type": 1,
                    "message_id": 8,
                    "from_user_id": "user-a",
                    "context_token": "ctx-2",
                    "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                }],
            })

            messages = client.poll()
            self.assertEqual([item.key for item in messages], ["ilink:8"])
            self.assertEqual(client.target_for("user-a"), ReplyTarget("user-a", "ctx-2"))
            client.acknowledge("ilink:8")

            restored = self.make_client(root)
            self.assertEqual(restored._state["cursor"], "next")
            self.assertEqual(restored._state["pending"], [])
            self.assertIn("ilink:8", restored._state["seen"])

    def test_duplicate_inbound_message_cannot_replace_saved_context(self) -> None:
        with TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            original = {
                "message_type": 1,
                "message_id": 7,
                "from_user_id": "user-a",
                "context_token": "ctx-original",
                "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
            }
            client._record_response({"get_updates_buf": "one", "msgs": [original]})
            replay = {**original, "context_token": "ctx-replayed"}

            client._record_response({"get_updates_buf": "two", "msgs": [replay]})

            self.assertEqual(client._state["contexts"]["user-a"], "ctx-original")

    def test_failed_state_save_does_not_advance_cursor_or_hide_message(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            client = self.make_client(root)
            response = {
                "get_updates_buf": "next",
                "msgs": [{
                    "message_type": 1,
                    "message_id": 10,
                    "from_user_id": "user-a",
                    "context_token": "ctx-10",
                    "item_list": [{"type": 1, "text_item": {"text": "不能卡住"}}],
                }],
            }
            save = client._store.save
            client._store.save = lambda _state: (_ for _ in ()).throw(OSError("disk"))

            with self.assertRaises(OSError):
                client._record_response(response)

            self.assertEqual(client._state["cursor"], "")
            self.assertEqual(client._state["pending"], [])
            self.assertEqual(client.poll(), [])

            client._store.save = save
            client._record_response(response)
            self.assertEqual([item.key for item in client.poll()], ["ilink:10"])

    def test_failed_ack_save_keeps_message_pending_for_retry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            client = self.make_client(root)
            client._record_response({
                "get_updates_buf": "next",
                "msgs": [{
                    "message_type": 1,
                    "message_id": 11,
                    "from_user_id": "user-a",
                    "item_list": [{"type": 1, "text_item": {"text": "保留我"}}],
                }],
            })
            self.assertEqual([item.key for item in client.poll()], ["ilink:11"])
            save = client._store.save
            client._store.save = lambda _state: (_ for _ in ()).throw(OSError("disk"))

            with self.assertRaises(OSError):
                client.acknowledge("ilink:11")

            self.assertEqual(
                [value["key"] for value in client._state["pending"]],
                ["ilink:11"],
            )
            self.assertNotIn("ilink:11", client._state["seen"])
            self.assertNotIn("ilink:11", client._queued_keys)

            client._store.save = save
            client.acknowledge("ilink:11")
            self.assertEqual(client._state["pending"], [])

    def test_retry_queue_filters_messages_acknowledged_after_requeue(self) -> None:
        with TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            client._record_response({
                "get_updates_buf": "next",
                "msgs": [{
                    "message_type": 1,
                    "message_id": 12,
                    "from_user_id": "user-a",
                    "item_list": [{"type": 1, "text_item": {"text": "一次"}}],
                }],
            })
            incoming = client.poll()[0]
            client.retry(incoming)
            client.retry(incoming)
            client.acknowledge(incoming.key)

            self.assertEqual(client.poll(), [])

    def test_account_binding_rejects_cross_bot_state_reuse(self) -> None:
        with TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            client._bind_account_state("bot-old")

            with self.assertRaisesRegex(ILinkProtocolError, "跨账号污染"):
                client._bind_account_state("bot-new")

            self.assertEqual(client._state["account_id"], "bot-old")

    def test_malformed_message_array_does_not_advance_cursor(self) -> None:
        with TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))

            with self.assertRaisesRegex(ILinkProtocolError, "msgs"):
                client._record_response({"get_updates_buf": "next", "msgs": {}})

            self.assertEqual(client._state["cursor"], "")

    def test_unsupported_inbound_content_is_recorded_as_dead_letter(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            client = self.make_client(root)
            client._record_response({
                "get_updates_buf": "next",
                "msgs": [{
                    "message_type": 1,
                    "message_id": 13,
                    "from_user_id": "user-a",
                    "item_list": [{"type": 4, "file_item": {"name": "x"}}],
                }],
            })

            self.assertEqual(client.poll(), [])
            self.assertEqual(client._state["cursor"], "next")
            self.assertEqual(client.inbound_dead_letter_count, 1)
            restored = self.make_client(root)
            self.assertEqual(restored.inbound_dead_letter_count, 1)

    def test_poll_loop_counts_state_checkpoint_failures_and_stops(self) -> None:
        with TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            api = PollAPI()
            client._api = api
            client._stop_event = FastEvent(api)
            client._store.save = lambda _state: (_ for _ in ()).throw(OSError("disk"))

            client._poll_loop()

            self.assertEqual(api.calls, 3)
            self.assertIsInstance(client._worker_error, OSError)

    def test_send_uses_target_context_and_unique_chunks(self) -> None:
        with TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            api = FakeAPI()
            client._api = api

            client.send("a" * 130, ReplyTarget("user-a", "ctx"))

            self.assertEqual(len(api.messages), 2)
            self.assertTrue(all(item["to_user_id"] == "user-a" for item in api.messages))
            self.assertTrue(all(item["context_token"] == "ctx" for item in api.messages))
            self.assertNotEqual(api.messages[0]["client_id"], api.messages[1]["client_id"])
            self.assertTrue(api.messages[0]["item_list"][0]["text_item"]["text"].startswith("[Bot] (1/2)"))

    def test_send_retry_reuses_stable_client_ids_for_each_chunk(self) -> None:
        with TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            api = FakeAPI()
            client._api = api

            for _ in range(2):
                client.send(
                    "a" * 130,
                    ReplyTarget("user-a", "ctx"),
                    client_id="outbox-item-1",
                )

            first = [item["client_id"] for item in api.messages[:2]]
            second = [item["client_id"] for item in api.messages[2:]]
            self.assertEqual(first, second)
            self.assertEqual(len(set(first)), 2)

    def test_send_caps_pathological_text_reply_chunk_count(self) -> None:
        with TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            api = FakeAPI()
            client._api = api

            client.send("a" * 100_000, ReplyTarget("user-a", "ctx"))

            self.assertEqual(len(api.messages), 20)
            final_text = api.messages[-1]["item_list"][0]["text_item"]["text"]
            self.assertIn("回复过长，已截断", final_text)

    def test_send_image_uploads_and_sends_type_two_item(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "reply.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nreply")
            client = self.make_client(root)
            api = FakeAPI()
            client._api = api

            client.send_image(image_path, ReplyTarget("user-a", "ctx-image"))

            self.assertEqual(len(api.messages), 1)
            message = api.messages[0]
            self.assertEqual(message["to_user_id"], "user-a")
            self.assertEqual(message["context_token"], "ctx-image")
            self.assertEqual(message["item_list"][0]["type"], 2)
            self.assertEqual(
                message["item_list"][0]["image_item"]["media"]["encrypt_query_param"],
                "cdn-user-a",
            )

    def test_send_file_uploads_and_sends_type_four_item(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "conversation.md"
            file_path.write_text("# 对话", encoding="utf-8")
            client = self.make_client(root)
            api = FakeAPI()
            client._api = api

            client.send_file(file_path, ReplyTarget("user-a", "ctx-file"))

            message = api.messages[0]
            self.assertEqual(message["context_token"], "ctx-file")
            self.assertEqual(message["item_list"][0]["type"], 4)
            self.assertEqual(
                message["item_list"][0]["file_item"]["file_name"],
                "conversation.md",
            )

    def test_default_target_cannot_be_redirected_by_last_seen_user(self) -> None:
        with TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            client._state["last_user_id"] = "untrusted"
            client._state["contexts"] = {"owner": "owner-ctx", "untrusted": "bad-ctx"}

            self.assertEqual(client.default_target(), ReplyTarget("owner", "owner-ctx"))

    def test_split_text_drops_empty_payload(self) -> None:
        self.assertEqual(split_text("   ", 100), [])


if __name__ == "__main__":
    unittest.main()
