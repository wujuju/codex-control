import json
import unittest
from unittest.mock import patch

from wechat_codex.wecom_bot import (
    _decode_frame,
    _subscribe,
    response_payload,
    subscribe_payload,
)


class FakeWebSocket:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return next(self.responses)


class WeComBotTests(unittest.IsolatedAsyncioTestCase):
    def test_subscribe_payload_uses_official_command(self) -> None:
        payload = subscribe_payload("bot-id", "secret", "req-1")
        self.assertEqual(payload["cmd"], "aibot_subscribe")
        self.assertEqual(payload["body"], {"bot_id": "bot-id", "secret": "secret"})

    def test_response_is_finished_stream(self) -> None:
        with patch("wechat_codex.wecom_bot.uuid.uuid4") as make_uuid:
            make_uuid.return_value.hex = "stream-id"
            payload = response_payload("req-1", "测试回复")
        self.assertEqual(payload["cmd"], "aibot_respond_msg")
        self.assertEqual(payload["headers"]["req_id"], "req-1")
        self.assertEqual(
            payload["body"]["stream"],
            {"id": "stream-id", "finish": True, "content": "测试回复"},
        )

    def test_decode_rejects_non_object_json(self) -> None:
        with self.assertRaises(RuntimeError):
            _decode_frame("[]")

    async def test_subscribe_accepts_success_response(self) -> None:
        websocket = FakeWebSocket(
            [json.dumps({"headers": {"req_id": "req-fixed"}, "errcode": 0})]
        )
        with patch("wechat_codex.wecom_bot._request_id", return_value="req-fixed"):
            await _subscribe(websocket, "bot-id", "secret", 1)
        self.assertEqual(websocket.sent[0]["cmd"], "aibot_subscribe")


if __name__ == "__main__":
    unittest.main()
