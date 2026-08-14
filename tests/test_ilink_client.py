from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

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


class ILinkClientTests(unittest.TestCase):
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
