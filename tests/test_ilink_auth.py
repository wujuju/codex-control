from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from wechat_codex.ilink_auth import (
    ILinkLoginCancelled,
    load_credentials,
    login_with_qr,
)


class FakeLoginAPI:
    instances = []

    def __init__(self, base_url, token=None):
        self.base_url = base_url
        self.closed = False
        self.statuses = [
            {"status": "scaned"},
            {
                "status": "confirmed",
                "bot_token": "token-1",
                "ilink_bot_id": "bot-1",
                "ilink_user_id": "owner-1",
                "baseurl": "https://ilinkai.weixin.qq.com",
            },
        ]
        self.__class__.instances.append(self)

    def create_qr_code(self, local_tokens):
        return {"qrcode": "qr-1", "qrcode_img_content": "https://weixin.qq/qr"}

    def get_qr_status(self, qrcode, verify_code=None):
        return self.statuses.pop(0)

    def close(self):
        self.closed = True


class ILinkAuthTests(unittest.TestCase):
    def test_confirmed_login_is_persisted(self) -> None:
        FakeLoginAPI.instances.clear()
        with TemporaryDirectory() as directory, patch(
            "wechat_codex.ilink_auth._show_qr"
        ):
            path = Path(directory) / "account.json"
            credentials = login_with_qr(
                path,
                force=True,
                output=lambda _text: None,
                sleep=lambda _seconds: None,
                api_factory=FakeLoginAPI,
            )

            self.assertEqual(credentials.account_id, "bot-1")
            self.assertEqual(load_credentials(path), credentials)
            self.assertTrue(FakeLoginAPI.instances[0].closed)

    def test_qr_content_is_reported_to_gui_callback(self) -> None:
        received: list[str] = []
        output: list[str] = []
        with TemporaryDirectory() as directory:
            login_with_qr(
                Path(directory) / "account.json",
                force=True,
                output=output.append,
                sleep=lambda _seconds: None,
                api_factory=FakeLoginAPI,
                qr_callback=received.append,
            )

        self.assertEqual(received, ["https://weixin.qq/qr"])
        self.assertNotIn("https://weixin.qq/qr", output)

    def test_login_can_be_cancelled_while_waiting_for_scan(self) -> None:
        class WaitingAPI(FakeLoginAPI):
            def get_qr_status(self, qrcode, verify_code=None):
                return {"status": "wait"}

        checks = iter((False, True))
        with TemporaryDirectory() as directory, patch(
            "wechat_codex.ilink_auth._show_qr"
        ):
            with self.assertRaises(ILinkLoginCancelled):
                login_with_qr(
                    Path(directory) / "account.json",
                    force=True,
                    output=lambda _text: None,
                    sleep=lambda _seconds: None,
                    api_factory=WaitingAPI,
                    cancelled=lambda: next(checks),
                )


if __name__ == "__main__":
    unittest.main()
