import base64
import unittest

import httpx

from wechat_codex.ilink_api import (
    ILinkAPI,
    ILinkAuthenticationError,
    ILINK_APP_CLIENT_VERSION,
)


class ILinkAPITests(unittest.TestCase):
    def test_get_updates_sends_required_headers_and_base_info(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, json={"ret": 0, "msgs": [], "get_updates_buf": "next"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        api = ILinkAPI("https://ilinkai.weixin.qq.com", "secret", client=client)

        result = api.get_updates("cursor", 40)

        request = captured["request"]
        self.assertEqual(result["get_updates_buf"], "next")
        self.assertEqual(request.headers["authorization"], "Bearer secret")
        self.assertEqual(request.headers["authorizationtype"], "ilink_bot_token")
        self.assertEqual(request.headers["ilink-app-id"], "bot")
        self.assertEqual(
            request.headers["ilink-app-clientversion"], str(ILINK_APP_CLIENT_VERSION)
        )
        decoded_uin = base64.b64decode(request.headers["x-wechat-uin"]).decode("ascii")
        self.assertTrue(decoded_uin.isdigit())
        body = __import__("json").loads(request.content)
        self.assertEqual(body["get_updates_buf"], "cursor")
        self.assertEqual(body["base_info"]["channel_version"], "2.4.6")

    def test_stale_token_is_reported_as_authentication_error(self) -> None:
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"ret": -14, "errmsg": "stale"})
            )
        )
        api = ILinkAPI("https://ilinkai.weixin.qq.com", "secret", client=client)

        with self.assertRaisesRegex(ILinkAuthenticationError, "重新运行 ilink-login"):
            api.get_updates("", 40)


if __name__ == "__main__":
    unittest.main()
