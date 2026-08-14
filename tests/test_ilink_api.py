import base64
import json
import unittest

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from wechat_codex.ilink_api import (
    ILinkAPI,
    ILinkAuthenticationError,
    ILINK_APP_CLIENT_VERSION,
)


class ILinkAPITests(unittest.TestCase):
    def test_upload_image_encrypts_payload_and_returns_image_item(self) -> None:
        plaintext = b"\x89PNG\r\n\x1a\noutbound-image"
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/getuploadurl"):
                captured["metadata"] = json.loads(request.content)
                return httpx.Response(200, json={"ret": 0, "upload_param": "upload+/="})
            captured["upload_url"] = str(request.url)
            captured["ciphertext"] = request.content
            return httpx.Response(200, headers={"x-encrypted-param": "download-token"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        api = ILinkAPI("https://ilinkai.weixin.qq.com", "secret", client=client)

        image_item = api.upload_image(plaintext, "user-a")

        metadata = captured["metadata"]
        aes_key = bytes.fromhex(metadata["aeskey"])
        decrypted = unpad(
            AES.new(aes_key, AES.MODE_ECB).decrypt(captured["ciphertext"]),
            AES.block_size,
        )
        self.assertEqual(decrypted, plaintext)
        self.assertEqual(metadata["to_user_id"], "user-a")
        self.assertEqual(metadata["rawsize"], len(plaintext))
        self.assertEqual(metadata["filesize"], len(captured["ciphertext"]))
        self.assertIn("encrypted_query_param=upload%2B%2F%3D", captured["upload_url"])
        self.assertEqual(image_item["media"]["encrypt_query_param"], "download-token")
        self.assertEqual(
            base64.b64decode(image_item["media"]["aes_key"]).decode("ascii"),
            metadata["aeskey"],
        )
        self.assertEqual(image_item["mid_size"], len(captured["ciphertext"]))

    def test_download_image_decrypts_weixin_cdn_payload(self) -> None:
        key = bytes.fromhex("00112233445566778899aabbccddeeff")
        plaintext = b"\x89PNG\r\n\x1a\nimage-data"
        ciphertext = AES.new(key, AES.MODE_ECB).encrypt(pad(plaintext, AES.block_size))
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, content=ciphertext)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        api = ILinkAPI("https://ilinkai.weixin.qq.com", "secret", client=client)

        result = api.download_image({
            "aeskey": key.hex(),
            "media": {"encrypt_query_param": "encrypted+/="},
        })

        self.assertEqual(result, plaintext)
        self.assertIn("encrypted_query_param=encrypted%2B%2F%3D", captured["url"])

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
