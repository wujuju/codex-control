from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


log = logging.getLogger(__name__)

DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.6"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (4 << 8) | 6
BOT_AGENT = "WechatCodexControl/0.1.0"
STALE_TOKEN_ERRCODE = -14
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
MAX_INBOUND_IMAGE_BYTES = 20 * 1024 * 1024
MAX_OUTBOUND_IMAGE_BYTES = 20 * 1024 * 1024
MAX_OUTBOUND_FILE_BYTES = 20 * 1024 * 1024


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


def _validate_weixin_download_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ILinkProtocolError("微信图片 CDN 返回了无效下载地址")
    hostname = parsed.hostname.lower()
    if hostname != "weixin.qq.com" and not hostname.endswith(".weixin.qq.com"):
        raise ILinkProtocolError("微信图片下载地址不是微信官方域名")
    return value


def _decode_image_aes_key(image_item: dict[str, Any], media: dict[str, Any]) -> bytes | None:
    raw_hex = str(image_item.get("aeskey") or "").strip()
    if raw_hex:
        try:
            key = bytes.fromhex(raw_hex)
        except ValueError as exc:
            raise ILinkProtocolError("微信图片 AES 密钥不是有效十六进制") from exc
        if len(key) != 16:
            raise ILinkProtocolError("微信图片 AES 密钥长度不是 16 字节")
        return key

    encoded = str(media.get("aes_key") or "").strip()
    if not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ILinkProtocolError("微信图片 AES 密钥不是有效 Base64") from exc
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            key = bytes.fromhex(decoded.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ILinkProtocolError("微信图片 AES 密钥编码无效") from exc
        if len(key) == 16:
            return key
    raise ILinkProtocolError("微信图片 AES 密钥长度不是 16 字节")


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

    def upload_image(self, payload: bytes, to_user_id: str) -> dict[str, Any]:
        """Encrypt and upload an image, returning an iLink ImageItem."""
        if not payload:
            raise ILinkProtocolError("待发送的图片内容为空")
        if len(payload) > MAX_OUTBOUND_IMAGE_BYTES:
            raise ILinkProtocolError("待发送的图片超过 20 MB 限制")
        if not to_user_id.strip():
            raise ILinkProtocolError("图片消息缺少接收用户")

        file_key = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        encrypted = AES.new(aes_key, AES.MODE_ECB).encrypt(
            pad(payload, AES.block_size)
        )
        response = self._client.post(
            self._url("ilink/bot/getuploadurl"),
            headers=self._headers(),
            json={
                "filekey": file_key,
                "media_type": 1,
                "to_user_id": to_user_id,
                "rawsize": len(payload),
                "rawfilemd5": hashlib.md5(payload).hexdigest(),
                "filesize": len(encrypted),
                "no_need_thumb": True,
                "aeskey": aes_key.hex(),
                "base_info": self.base_info(),
            },
            timeout=httpx.Timeout(15.0, connect=10.0),
        )
        upload_info = self._decode(response, "获取微信图片上传地址")
        self._check_api_result(upload_info, "获取微信图片上传地址")
        upload_param = str(upload_info.get("upload_param") or "").strip()
        full_url = str(upload_info.get("upload_full_url") or "").strip()
        if full_url:
            upload_url = _validate_weixin_download_url(full_url)
        elif upload_param:
            upload_url = (
                f"{DEFAULT_CDN_BASE_URL}/upload?encrypted_query_param="
                f"{quote(upload_param, safe='')}&filekey={quote(file_key, safe='')}"
            )
        else:
            raise ILinkProtocolError("微信图片上传地址缺少 CDN 参数")

        download_param = self._upload_cdn(upload_url, encrypted)
        return {
            "media": {
                "encrypt_query_param": download_param,
                "aes_key": base64.b64encode(
                    aes_key.hex().encode("ascii")
                ).decode("ascii"),
                "encrypt_type": 1,
            },
            "mid_size": len(encrypted),
        }

    def upload_file(
        self,
        payload: bytes,
        to_user_id: str,
        file_name: str,
    ) -> dict[str, Any]:
        """Encrypt and upload a generic file, returning an iLink FileItem."""
        if not payload:
            raise ILinkProtocolError("待发送的文件内容为空")
        if len(payload) > MAX_OUTBOUND_FILE_BYTES:
            raise ILinkProtocolError("待发送的文件超过 20 MB 限制")
        if not to_user_id.strip():
            raise ILinkProtocolError("文件消息缺少接收用户")
        cleaned_name = "".join(
            character for character in file_name.strip() if ord(character) >= 32
        )[:120]
        if not cleaned_name:
            raise ILinkProtocolError("文件消息缺少文件名")

        file_key = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        encrypted = AES.new(aes_key, AES.MODE_ECB).encrypt(
            pad(payload, AES.block_size)
        )
        plain_md5 = hashlib.md5(payload).hexdigest()
        response = self._client.post(
            self._url("ilink/bot/getuploadurl"),
            headers=self._headers(),
            json={
                "filekey": file_key,
                "media_type": 3,
                "to_user_id": to_user_id,
                "rawsize": len(payload),
                "rawfilemd5": plain_md5,
                "filesize": len(encrypted),
                "no_need_thumb": True,
                "aeskey": aes_key.hex(),
                "base_info": self.base_info(),
            },
            timeout=httpx.Timeout(15.0, connect=10.0),
        )
        upload_info = self._decode(response, "获取微信文件上传地址")
        self._check_api_result(upload_info, "获取微信文件上传地址")
        upload_param = str(upload_info.get("upload_param") or "").strip()
        full_url = str(upload_info.get("upload_full_url") or "").strip()
        if full_url:
            upload_url = _validate_weixin_download_url(full_url)
        elif upload_param:
            upload_url = (
                f"{DEFAULT_CDN_BASE_URL}/upload?encrypted_query_param="
                f"{quote(upload_param, safe='')}&filekey={quote(file_key, safe='')}"
            )
        else:
            raise ILinkProtocolError("微信文件上传地址缺少 CDN 参数")

        download_param = self._upload_cdn(upload_url, encrypted)
        return {
            "media": {
                "encrypt_query_param": download_param,
                "aes_key": base64.b64encode(
                    aes_key.hex().encode("ascii")
                ).decode("ascii"),
                "encrypt_type": 1,
            },
            "file_name": cleaned_name,
            "md5": plain_md5,
            "len": str(len(payload)),
        }

    def _upload_cdn(self, url: str, encrypted: bytes) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.post(
                    url,
                    headers={"Content-Type": "application/octet-stream"},
                    content=encrypted,
                    timeout=httpx.Timeout(60.0, connect=10.0),
                )
                if 400 <= response.status_code < 500:
                    response.raise_for_status()
                if response.status_code != 200:
                    raise ILinkProtocolError(
                        f"微信图片 CDN 上传失败：HTTP {response.status_code}"
                    )
                download_param = str(
                    response.headers.get("x-encrypted-param") or ""
                ).strip()
                if not download_param:
                    raise ILinkProtocolError("微信图片 CDN 未返回下载参数")
                return download_param
            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500:
                    raise ILinkProtocolError(
                        f"微信图片 CDN 上传失败：HTTP {exc.response.status_code}"
                    ) from exc
                last_error = exc
            except ILinkProtocolError as exc:
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt < 2:
                log.warning("微信图片 CDN 上传失败，准备重试（%d/3）：%s", attempt + 1, last_error)
        raise ILinkProtocolError(f"微信图片 CDN 上传失败：{last_error}") from last_error

    def download_image(self, image_item: dict[str, Any]) -> bytes:
        media = image_item.get("media") or image_item.get("thumb_media") or {}
        if not isinstance(media, dict):
            raise ILinkProtocolError("微信图片缺少有效 CDN 引用")
        full_url = str(media.get("full_url") or image_item.get("url") or "").strip()
        query_param = str(media.get("encrypt_query_param") or "").strip()
        if full_url:
            url = _validate_weixin_download_url(full_url)
        elif query_param:
            url = (
                f"{DEFAULT_CDN_BASE_URL}/download?encrypted_query_param="
                f"{quote(query_param, safe='')}"
            )
        else:
            raise ILinkProtocolError("微信图片缺少 CDN 下载参数")

        try:
            with self._client.stream(
                "GET",
                url,
                headers={"Accept-Encoding": "identity"},
                timeout=httpx.Timeout(30.0, connect=10.0),
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_INBOUND_IMAGE_BYTES + AES.block_size:
                        raise ILinkProtocolError("微信图片超过 20 MB 限制")
                    chunks.append(chunk)
        except ILinkProtocolError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ILinkProtocolError(
                f"微信图片 CDN 下载失败：HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ILinkProtocolError(f"微信图片 CDN 下载失败：{exc}") from exc

        payload = b"".join(chunks)
        key = _decode_image_aes_key(image_item, media)
        if key is not None:
            if not payload or len(payload) % AES.block_size:
                raise ILinkProtocolError("微信图片密文长度无效")
            try:
                payload = unpad(AES.new(key, AES.MODE_ECB).decrypt(payload), AES.block_size)
            except ValueError as exc:
                raise ILinkProtocolError("微信图片 AES 解密失败") from exc
        if not payload:
            raise ILinkProtocolError("微信图片内容为空")
        if len(payload) > MAX_INBOUND_IMAGE_BYTES:
            raise ILinkProtocolError("微信图片超过 20 MB 限制")
        return payload

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
