from __future__ import annotations

import base64
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


log = logging.getLogger(__name__)

DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.6"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (4 << 8) | 6
BOT_AGENT = "WechatCodexControl/0.1.0"
STALE_TOKEN_ERRCODE = -14


class ILinkError(RuntimeError):
    pass


class ILinkProtocolError(ILinkError):
    pass


class ILinkAuthenticationError(ILinkError):
    pass


@dataclass(frozen=True)
class ILinkCredentials:
    token: str
    account_id: str
    user_id: str
    base_url: str


def validate_base_url(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ILinkProtocolError(f"无效的 iLink HTTPS 地址：{value!r}")
    hostname = parsed.hostname.lower()
    if hostname != "weixin.qq.com" and not hostname.endswith(".weixin.qq.com"):
        raise ILinkProtocolError(f"iLink 地址不是微信官方域名：{hostname}")
    return cleaned


def _random_wechat_uin() -> str:
    value = secrets.randbits(32)
    return base64.b64encode(str(value).encode("ascii")).decode("ascii")


class ILinkAPI:
    def __init__(
        self,
        base_url: str = DEFAULT_API_BASE_URL,
        token: str | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self.token = token.strip() if token else None
        self._client = client or httpx.Client(follow_redirects=False)
        self._owns_client = client is None

    @staticmethod
    def base_info() -> dict[str, str]:
        return {
            "channel_version": CHANNEL_VERSION,
            "bot_agent": BOT_AGENT,
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _common_headers() -> dict[str, str]:
        return {
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_wechat_uin(),
            **self._common_headers(),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _url(self, endpoint: str) -> str:
        return urljoin(self.base_url + "/", endpoint.lstrip("/"))

    @staticmethod
    def _decode(response: httpx.Response, label: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ILinkProtocolError(
                f"{label} HTTP {response.status_code}"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise ILinkProtocolError(f"{label} 返回了无效 JSON") from exc
        if not isinstance(data, dict):
            raise ILinkProtocolError(f"{label} 返回值不是 JSON 对象")
        return data

    @staticmethod
    def _check_api_result(data: dict[str, Any], label: str) -> None:
        code = data.get("errcode", data.get("ret", 0))
        if code in (None, 0):
            return
        detail = str(data.get("errmsg") or "").strip()
        if code == STALE_TOKEN_ERRCODE:
            raise ILinkAuthenticationError(
                "iLink 登录凭证已失效，请重新运行 ilink-login"
            )
        raise ILinkProtocolError(
            f"{label} 失败：code={code}" + (f"，{detail}" if detail else "")
        )

    def create_qr_code(self, local_tokens: list[str]) -> dict[str, Any]:
        response = self._client.post(
            self._url("ilink/bot/get_bot_qrcode?bot_type=3"),
            headers=self._headers(),
            json={"local_token_list": local_tokens[-10:]},
            timeout=httpx.Timeout(15.0, connect=10.0),
        )
        return self._decode(response, "获取 iLink 二维码")

    def get_qr_status(
        self,
        qrcode: str,
        verify_code: str | None = None,
    ) -> dict[str, Any]:
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        try:
            response = self._client.get(
                self._url("ilink/bot/get_qrcode_status"),
                headers=self._common_headers(),
                params=params,
                timeout=httpx.Timeout(40.0, connect=10.0),
            )
        except httpx.ReadTimeout:
            return {"status": "wait"}
        return self._decode(response, "查询 iLink 扫码状态")

    def get_updates(
        self,
        cursor: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        try:
            response = self._client.post(
                self._url("ilink/bot/getupdates"),
                headers=self._headers(),
                json={
                    "get_updates_buf": cursor,
                    "base_info": self.base_info(),
                },
                timeout=httpx.Timeout(
                    timeout_seconds + 5.0,
                    connect=10.0,
                    write=10.0,
                    pool=10.0,
                ),
            )
        except httpx.ReadTimeout:
            return {"ret": 0, "msgs": [], "get_updates_buf": cursor}
        data = self._decode(response, "iLink 长轮询")
        self._check_api_result(data, "iLink 长轮询")
        return data

    def send_message(self, message: dict[str, Any]) -> None:
        response = self._client.post(
            self._url("ilink/bot/sendmessage"),
            headers=self._headers(),
            json={"msg": message, "base_info": self.base_info()},
            timeout=httpx.Timeout(15.0, connect=10.0),
        )
        data = self._decode(response, "发送 iLink 消息")
        self._check_api_result(data, "发送 iLink 消息")

    def notify_start(self) -> None:
        self._notify("ilink/bot/msg/notifystart", "启动通知")

    def notify_stop(self) -> None:
        self._notify("ilink/bot/msg/notifystop", "停止通知")

    def _notify(self, endpoint: str, label: str) -> None:
        response = self._client.post(
            self._url(endpoint),
            headers=self._headers(),
            json={"base_info": self.base_info()},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        data = self._decode(response, label)
        self._check_api_result(data, label)
