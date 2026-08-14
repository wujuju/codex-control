from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wechat_codex.ilink_api import ILinkCredentials
from wechat_codex.onboarding import ensure_logins


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        ilink_credentials_file=Path("account.json"),
        ilink_api_base_url="https://ilinkai.weixin.qq.com",
        chatgpt_profile_dir=Path("profile"),
        chatgpt_browser_channel="chrome",
        chatgpt_proxy_server=None,
    )


class EnsureLoginsTests(unittest.TestCase):
    @patch("wechat_codex.onboarding.login_chatgpt")
    @patch("wechat_codex.onboarding.has_chatgpt_plus_login", return_value=True)
    @patch("wechat_codex.onboarding.login_with_qr")
    @patch("wechat_codex.onboarding.has_valid_ilink_login", return_value=True)
    @patch("wechat_codex.onboarding.load_credentials")
    def test_existing_logins_do_not_open_login_flows(
        self,
        load_credentials: Mock,
        has_valid_ilink_login: Mock,
        login_with_qr: Mock,
        has_chatgpt_login: Mock,
        login_chatgpt: Mock,
    ) -> None:
        load_credentials.return_value = ILinkCredentials(
            token="token",
            account_id="bot-1",
            user_id="owner-1",
            base_url="https://ilinkai.weixin.qq.com",
        )

        ensure_logins(_config(), output=lambda _text: None)

        login_with_qr.assert_not_called()
        has_valid_ilink_login.assert_called_once()
        has_chatgpt_login.assert_called_once()
        login_chatgpt.assert_not_called()

    @patch("wechat_codex.onboarding.login_chatgpt")
    @patch("wechat_codex.onboarding.has_chatgpt_plus_login", return_value=False)
    @patch("wechat_codex.onboarding.login_with_qr")
    @patch("wechat_codex.onboarding.has_valid_ilink_login")
    @patch("wechat_codex.onboarding.load_credentials", return_value=None)
    def test_missing_logins_are_completed_in_order(
        self,
        _load_credentials: Mock,
        has_valid_ilink_login: Mock,
        login_with_qr: Mock,
        _has_chatgpt_login: Mock,
        login_chatgpt: Mock,
    ) -> None:
        calls: list[str] = []

        def complete_ilink(*args, **kwargs) -> ILinkCredentials:
            calls.append("ilink")
            return ILinkCredentials(
                token="token",
                account_id="bot-1",
                user_id="owner-1",
                base_url="https://ilinkai.weixin.qq.com",
            )

        login_with_qr.side_effect = complete_ilink
        login_chatgpt.side_effect = lambda *args, **kwargs: calls.append("chatgpt")

        ensure_logins(_config(), output=lambda _text: None)

        self.assertEqual(calls, ["ilink", "chatgpt"])
        has_valid_ilink_login.assert_not_called()

    @patch("wechat_codex.onboarding.login_chatgpt")
    @patch("wechat_codex.onboarding.has_chatgpt_plus_login", return_value=True)
    @patch("wechat_codex.onboarding.login_with_qr")
    @patch("wechat_codex.onboarding.has_valid_ilink_login", return_value=False)
    @patch("wechat_codex.onboarding.load_credentials")
    def test_expired_ilink_login_forces_new_qr(
        self,
        load_credentials: Mock,
        _has_valid_ilink_login: Mock,
        login_with_qr: Mock,
        _has_chatgpt_login: Mock,
        _login_chatgpt: Mock,
    ) -> None:
        expired = ILinkCredentials(
            token="expired",
            account_id="bot-old",
            user_id="owner-1",
            base_url="https://ilinkai.weixin.qq.com",
        )
        renewed = ILinkCredentials(
            token="new",
            account_id="bot-new",
            user_id="owner-1",
            base_url="https://ilinkai.weixin.qq.com",
        )
        load_credentials.return_value = expired
        login_with_qr.return_value = renewed

        ensure_logins(_config(), output=lambda _text: None)

        self.assertTrue(login_with_qr.call_args.kwargs["force"])


if __name__ == "__main__":
    unittest.main()
