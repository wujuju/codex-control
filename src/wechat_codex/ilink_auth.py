from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from .ilink_api import (
    DEFAULT_API_BASE_URL,
    ILinkAPI,
    ILinkCredentials,
    ILinkProtocolError,
    validate_base_url,
)
from .state_io import atomic_write_text


class ILinkLoginCancelled(ILinkProtocolError):
    """Raised when an interactive QR login is cancelled by its owner."""


def load_credentials(path: Path) -> ILinkCredentials | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        credentials = ILinkCredentials(
            token=str(raw["token"]).strip(),
            account_id=str(raw["account_id"]).strip(),
            user_id=str(raw["user_id"]).strip(),
            base_url=validate_base_url(str(raw["base_url"])),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ILinkProtocolError(f"iLink 凭证文件无效：{path}") from exc
    if not credentials.token or not credentials.account_id or not credentials.user_id:
        raise ILinkProtocolError(f"iLink 凭证文件字段不完整：{path}")
    return credentials


def save_credentials(path: Path, credentials: ILinkCredentials) -> None:
    atomic_write_text(
        path,
        json.dumps(
            {
                "token": credentials.token,
                "account_id": credentials.account_id,
                "user_id": credentials.user_id,
                "base_url": credentials.base_url,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def _show_qr(
    content: str,
    output: Callable[[str], None],
    qr_callback: Callable[[str], None] | None = None,
) -> None:
    if qr_callback is not None:
        qr_callback(content)
        output("请用手机微信扫描二维码并确认授权：")
        return
    output("请用手机微信扫描二维码并确认授权：")
    try:
        import segno

        qr = segno.make(content)
        qr.terminal(compact=True)
    except Exception:
        output("终端二维码生成失败，请使用下面的二维码内容：")
    output(content)


def login_with_qr(
    credentials_path: Path,
    *,
    api_base_url: str = DEFAULT_API_BASE_URL,
    force: bool = False,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    api_factory: Callable[..., ILinkAPI] = ILinkAPI,
    qr_callback: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ILinkCredentials:
    existing = load_credentials(credentials_path)
    if existing is not None and not force:
        output(f"已存在 iLink 登录凭证：{existing.account_id}")
        output("如需重新扫码，请添加 --force。")
        return existing

    local_tokens = [existing.token] if existing is not None else []
    fixed_url = validate_base_url(api_base_url)
    api = api_factory(fixed_url)
    opened_apis = [api]
    deadline = time.monotonic() + 8 * 60
    try:
        qr_response = api.create_qr_code(local_tokens)
        qrcode = str(qr_response.get("qrcode") or "").strip()
        qr_content = str(qr_response.get("qrcode_img_content") or "").strip()
        if not qrcode or not qr_content:
            raise ILinkProtocolError("iLink 未返回有效二维码")
        _show_qr(qr_content, output, qr_callback)

        verify_code: str | None = None
        refresh_count = 0
        current_api = api
        while True:
            if cancelled is not None and cancelled():
                raise ILinkLoginCancelled("已取消微信登录")
            if time.monotonic() >= deadline:
                raise ILinkProtocolError("iLink 登录等待超时，请重新运行 ilink-login")
            status_response = current_api.get_qr_status(qrcode, verify_code)
            if cancelled is not None and cancelled():
                raise ILinkLoginCancelled("已取消微信登录")
            status = str(status_response.get("status") or "wait")
            if status == "wait":
                sleep(1)
                continue
            if status == "scaned":
                verify_code = None
                output("二维码已扫描，正在等待手机确认…")
                sleep(1)
                continue
            if status == "need_verifycode":
                verify_code = input_fn("请输入手机微信显示的数字：").strip()
                continue
            if status == "verify_code_blocked":
                raise ILinkProtocolError("配对数字多次输入错误，请稍后重新登录")
            if status == "scaned_but_redirect":
                redirect_host = str(status_response.get("redirect_host") or "").strip()
                parsed = urlparse("https://" + redirect_host)
                if not redirect_host or not parsed.hostname:
                    raise ILinkProtocolError("iLink 扫码跳转地址无效")
                current_api = api_factory(f"https://{redirect_host}")
                opened_apis.append(current_api)
                sleep(1)
                continue
            if status == "expired":
                refresh_count += 1
                if refresh_count >= 3:
                    raise ILinkProtocolError("iLink 二维码多次过期，请稍后重试")
                qr_response = api.create_qr_code(local_tokens)
                qrcode = str(qr_response.get("qrcode") or "").strip()
                qr_content = str(qr_response.get("qrcode_img_content") or "").strip()
                if not qrcode or not qr_content:
                    raise ILinkProtocolError("iLink 刷新二维码时未返回有效内容")
                _show_qr(qr_content, output, qr_callback)
                verify_code = None
                continue
            if status == "binded_redirect":
                if existing is None:
                    raise ILinkProtocolError("该 Bot 已绑定，但本机没有可恢复的凭证")
                output("该 Bot 已绑定，继续使用本机现有凭证。")
                return existing
            if status != "confirmed":
                raise ILinkProtocolError(f"未知的 iLink 登录状态：{status}")

            token = str(status_response.get("bot_token") or "").strip()
            account_id = str(status_response.get("ilink_bot_id") or "").strip()
            user_id = str(status_response.get("ilink_user_id") or "").strip()
            base_url = str(status_response.get("baseurl") or fixed_url).strip()
            if not token or not account_id or not user_id:
                raise ILinkProtocolError("iLink 登录成功响应缺少账号字段")
            credentials = ILinkCredentials(
                token=token,
                account_id=account_id,
                user_id=user_id,
                base_url=validate_base_url(base_url),
            )
            save_credentials(credentials_path, credentials)
            output(f"iLink 登录成功：{account_id}")
            return credentials
    finally:
        for opened_api in opened_apis:
            opened_api.close()
