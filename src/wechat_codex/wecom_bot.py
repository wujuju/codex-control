from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from .app import WeComBridge


log = logging.getLogger(__name__)
WECOM_WEBSOCKET_URL = "wss://openws.work.weixin.qq.com"


class WeComBotError(RuntimeError):
    pass


def _request_id() -> str:
    return uuid.uuid4().hex


def subscribe_payload(bot_id: str, secret: str, request_id: str) -> dict[str, Any]:
    return {
        "cmd": "aibot_subscribe",
        "headers": {"req_id": request_id},
        "body": {"bot_id": bot_id, "secret": secret},
    }


def response_payload(request_id: str, text: str) -> dict[str, Any]:
    return {
        "cmd": "aibot_respond_msg",
        "headers": {"req_id": request_id},
        "body": {
            "msgtype": "stream",
            "stream": {
                "id": uuid.uuid4().hex,
                "finish": True,
                "content": text,
            },
        },
    }


def _decode_frame(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeComBotError("企业微信长连接返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise WeComBotError("企业微信长连接返回的 JSON 顶层不是对象")
    return payload


async def _subscribe(
    websocket: Any,
    bot_id: str,
    secret: str,
    timeout_seconds: int,
) -> None:
    request_id = _request_id()
    await websocket.send(
        json.dumps(
            subscribe_payload(bot_id, secret, request_id),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    while True:
        raw = await asyncio.wait_for(websocket.recv(), timeout=timeout_seconds)
        response = _decode_frame(raw)
        headers = response.get("headers") or {}
        if headers.get("req_id") != request_id:
            log.debug("订阅期间忽略无关企业微信帧：%s", response.get("cmd") or headers)
            continue
        errcode = int(response.get("errcode", -1))
        if errcode != 0:
            raise WeComBotError(
                f"企业微信机器人订阅失败：{errcode} {response.get('errmsg', '')}"
            )
        return


def _default_connect(url: str, timeout_seconds: int) -> Any:
    from websockets.asyncio.client import connect

    return connect(
        url,
        ping_interval=None,
        open_timeout=timeout_seconds,
        close_timeout=5,
        max_size=4 * 1024 * 1024,
    )


async def check_bot_connection(
    url: str,
    bot_id: str,
    secret: str,
    timeout_seconds: int,
) -> None:
    async with _default_connect(url, timeout_seconds) as websocket:
        await _subscribe(websocket, bot_id, secret, timeout_seconds)


class WeComBotGateway:
    def __init__(
        self,
        bridge: WeComBridge,
        *,
        url: str,
        bot_id: str,
        secret: str,
        timeout_seconds: int,
        heartbeat_seconds: int = 30,
        connect_factory: Callable[[str, int], Any] | None = None,
    ) -> None:
        self.bridge = bridge
        self.url = url
        self.bot_id = bot_id
        self.secret = secret
        self.timeout_seconds = timeout_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._connect_factory = connect_factory or _default_connect
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready: asyncio.Event | None = None
        self._send_lock: asyncio.Lock | None = None
        self._websocket: Any | None = None
        self._pending: set[Future[None]] = set()

    def run_forever(self) -> None:
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            log.info("收到停止信号")
        finally:
            self.bridge.close()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()
        self._send_lock = asyncio.Lock()
        retry_seconds = 1
        while not self._stop_event.is_set():
            try:
                await self._connected_session()
                retry_seconds = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stop_event.is_set():
                    log.warning(
                        "企业微信机器人长连接中断，%s 秒后重连：%s",
                        retry_seconds,
                        exc,
                    )
                    await self._sleep_until_retry(retry_seconds)
                    retry_seconds = min(retry_seconds * 2, 30)

    def stop(self) -> None:
        self._stop_event.set()
        loop = self._loop
        websocket = self._websocket
        if loop and websocket:
            asyncio.run_coroutine_threadsafe(websocket.close(), loop)

    def respond(self, request_id: str, text: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise WeComBotError("企业微信机器人长连接尚未运行")
        future = asyncio.run_coroutine_threadsafe(
            self._send_when_connected(response_payload(request_id, text)),
            loop,
        )
        self._pending.add(future)
        future.add_done_callback(self._reply_completed)

    async def _connected_session(self) -> None:
        assert self._ready is not None
        async with self._connect_factory(self.url, self.timeout_seconds) as websocket:
            self._websocket = websocket
            try:
                await _subscribe(
                    websocket,
                    self.bot_id,
                    self.secret,
                    self.timeout_seconds,
                )
                self._ready.set()
                log.info("企业微信智能机器人长连接已建立，等待群内 @机器人 消息")
                heartbeat = asyncio.create_task(self._heartbeat())
                try:
                    async for raw in websocket:
                        self._handle_frame(_decode_frame(raw))
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            finally:
                self._ready.clear()
                if self._websocket is websocket:
                    self._websocket = None

    def _handle_frame(self, payload: dict[str, Any]) -> None:
        command = payload.get("cmd")
        if command == "aibot_msg_callback":
            try:
                self.bridge.submit(payload, self.respond)
            except Exception:
                log.exception("提交企业微信智能机器人回调失败")
            return
        if command == "aibot_event_callback":
            log.debug("忽略企业微信机器人事件回调：%s", payload)
            return
        if "errcode" in payload and int(payload.get("errcode", -1)) != 0:
            log.error(
                "企业微信长连接请求失败：req_id=%s errcode=%s errmsg=%s",
                (payload.get("headers") or {}).get("req_id"),
                payload.get("errcode"),
                payload.get("errmsg"),
            )

    async def _heartbeat(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(self.heartbeat_seconds)
            payload = {
                "cmd": "ping",
                "headers": {"req_id": _request_id()},
            }
            try:
                await self._send_now(payload)
            except Exception:
                log.debug("企业微信心跳发送失败", exc_info=True)
                return

    async def _send_when_connected(self, payload: dict[str, Any]) -> None:
        assert self._ready is not None
        while not self._stop_event.is_set():
            await self._ready.wait()
            try:
                await self._send_now(payload)
                return
            except Exception:
                self._ready.clear()
                log.warning("回复发送时连接已断开，将在重连后重试")
                await asyncio.sleep(0.2)
        raise WeComBotError("程序已停止，消息未发送")

    async def _send_now(self, payload: dict[str, Any]) -> None:
        websocket = self._websocket
        lock = self._send_lock
        if websocket is None or lock is None:
            raise WeComBotError("企业微信机器人长连接不可用")
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with lock:
            await websocket.send(serialized)

    async def _sleep_until_retry(self, seconds: int) -> None:
        for _ in range(seconds * 10):
            if self._stop_event.is_set():
                return
            await asyncio.sleep(0.1)

    def _reply_completed(self, future: Future[None]) -> None:
        self._pending.discard(future)
        if future.cancelled():
            return
        try:
            future.result()
        except Exception:
            log.exception("企业微信机器人回复最终发送失败")
