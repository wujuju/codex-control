from __future__ import annotations

from collections.abc import Callable

from .chatgpt_runner import has_chatgpt_plus_login, login_chatgpt
from .config import AppConfig
from .ilink_api import ILinkAPI, ILinkAuthenticationError, ILinkCredentials
from .ilink_auth import load_credentials, login_with_qr


def has_valid_ilink_login(credentials: ILinkCredentials) -> bool:
    """Validate the token with start/stop notifications, without consuming messages."""
    api = ILinkAPI(credentials.base_url, credentials.token)
    started = False
    try:
        api.notify_start()
        started = True
        return True
    except ILinkAuthenticationError:
        return False
    finally:
        if started:
            try:
                api.notify_stop()
            except Exception:
                pass
        api.close()


def ensure_logins(
    config: AppConfig,
    *,
    output: Callable[[str], None] = print,
) -> None:
    """Complete missing account logins in dependency order before startup."""
    credentials = load_credentials(config.ilink_credentials_file)
    credentials_expired = (
        credentials is not None and not has_valid_ilink_login(credentials)
    )
    if credentials is None or credentials_expired:
        if credentials_expired:
            output("微信 iLink 登录已失效，需要重新扫码授权。")
        output("尚未登录微信 iLink，请用微信扫描下面的二维码并确认授权。")
        credentials = login_with_qr(
            config.ilink_credentials_file,
            api_base_url=config.ilink_api_base_url,
            force=credentials_expired,
            output=output,
        )
    output(f"[OK] 微信 iLink 已登录：{credentials.account_id}")

    ensure_chatgpt_login(config, output=output)


def ensure_chatgpt_login(
    config: AppConfig,
    *,
    output: Callable[[str], None] = print,
) -> None:
    """Ensure the one application-level Plus profile is ready for all accounts."""
    output("正在检查 ChatGPT Plus 登录状态……")
    if not has_chatgpt_plus_login(
        config.chatgpt_profile_dir,
        config.chatgpt_browser_channel,
        config.chatgpt_proxy_server,
    ):
        output("尚未登录 ChatGPT Plus，正在打开登录浏览器。")
        login_chatgpt(
            config.chatgpt_profile_dir,
            config.chatgpt_browser_channel,
            config.chatgpt_proxy_server,
        )
    output("[OK] ChatGPT Plus 已登录")
