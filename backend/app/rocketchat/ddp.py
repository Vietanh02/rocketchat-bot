"""Minimal DDP (Meteor) client for Rocket.Chat inbound message streaming.

Why hand-rolled: the obvious PyPI option (rocketchat-async) is unmaintained and
targets Rocket.Chat 7.x. DDP itself is small, and we only need four verbs:
connect, login, sub, and ping/pong. We deliberately do NOT use the deprecated
realtime methods (loadHistory, getMessages, sendMessage) - outbound traffic goes
over REST instead, so a future 9.x removal only costs us this listener.

Protocol reference: https://developer.rocket.chat/apidocs/realtimeapi
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

# Subscribing to this pseudo-room id streams every message visible to the
# logged-in user, so the bot picks up new DMs without re-subscribing per room.
MY_MESSAGES = "__my_messages__"


@dataclass(slots=True)
class IncomingMessage:
    """One inbound chat message, normalised out of the DDP payload."""

    message_id: str
    room_id: str
    text: str
    sender_id: str | None
    sender_username: str | None
    room_type: str | None = None  # c=channel, p=private, d=direct, l=livechat
    thread_id: str | None = None
    is_edit: bool = False
    is_system: bool = False
    is_bot: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


def parse_message(args: list[Any]) -> IncomingMessage | None:
    """Turn a stream-room-messages event payload into an IncomingMessage."""
    if not args:
        return None
    msg = args[0]
    if not isinstance(msg, dict) or not msg.get("rid"):
        return None

    # args[1] carries room metadata (roomType, roomName, ...) on newer servers.
    meta = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
    user = msg.get("u") or {}

    return IncomingMessage(
        message_id=msg.get("_id", ""),
        room_id=msg["rid"],
        text=msg.get("msg", "") or "",
        sender_id=user.get("_id"),
        sender_username=user.get("username"),
        room_type=meta.get("roomType") or msg.get("t_room"),
        thread_id=msg.get("tmid"),
        is_edit=bool(msg.get("editedAt")),
        # "t" marks system events: user joined, room renamed, etc.
        is_system=bool(msg.get("t")),
        is_bot=bool(msg.get("bot")),
        raw=msg,
    )


class DDPClient:
    """Connects, logs in, streams messages, and reconnects with backoff."""

    def __init__(
        self,
        ws_url: str,
        *,
        auth_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        on_message: MessageHandler | None = None,
        ping_interval: float = 25.0,
        call_timeout: float = 20.0,
    ) -> None:
        if not (auth_token or (username and password)):
            raise ValueError("DDPClient needs either auth_token or username+password")
        self.ws_url = ws_url
        self.auth_token = auth_token
        self.username = username
        self.password = password
        self.on_message = on_message
        self.ping_interval = ping_interval
        self.call_timeout = call_timeout

        self.user_id: str | None = None
        self.connected = asyncio.Event()
        self.last_error: str | None = None

        self._ws: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._stop = asyncio.Event()

    # ----------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------- #

    @property
    def is_connected(self) -> bool:
        return self.connected.is_set()

    async def run_forever(self) -> None:
        """Keep a live session up until stop() is called."""
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._session()
                attempt = 0  # a clean session resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - we retry on anything
                self.last_error = str(exc)
                log.warning("DDP session ended: %s", exc)
            finally:
                self.connected.clear()
                self._fail_pending("connection closed")

            if self._stop.is_set():
                break
            attempt += 1
            delay = min(60.0, 2 ** min(attempt, 6)) * (0.5 + random.random() / 2)
            log.info("Reconnecting to %s in %.1fs", self.ws_url, delay)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    # ----------------------------------------------------------------- #
    # Session lifecycle
    # ----------------------------------------------------------------- #

    async def _session(self) -> None:
        log.info("Connecting to Rocket.Chat realtime API at %s", self.ws_url)
        async with websockets.connect(
            self.ws_url, ping_interval=None, max_size=4 * 1024 * 1024
        ) as ws:
            self._ws = ws
            await self._send({"msg": "connect", "version": "1", "support": ["1"]})

            reader = asyncio.create_task(self._read_loop(), name="ddp-reader")
            try:
                await self._await_connected(reader)
                await self._login()
                await self._subscribe()
                self.connected.set()
                self.last_error = None
                log.info("Bot listening as user %s", self.user_id)

                keepalive = asyncio.create_task(self._ping_loop(), name="ddp-ping")
                try:
                    done, _ = await asyncio.wait(
                        {reader, keepalive}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:  # surface the failure that ended the session
                        task.result()
                finally:
                    keepalive.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await keepalive
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
                self._ws = None

    async def _await_connected(self, reader: asyncio.Task) -> None:
        """Wait for the server's "connected" frame, or the reader's failure."""
        self._pending["__connected__"] = asyncio.get_running_loop().create_future()
        waiter = self._pending["__connected__"]
        done, _ = await asyncio.wait(
            {waiter, reader},
            timeout=self.call_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if reader in done:
            reader.result()
            raise ConnectionError("socket closed during handshake")
        if waiter not in done:
            raise TimeoutError("timed out waiting for DDP connect")
        waiter.result()

    async def _login(self) -> None:
        if self.auth_token:
            params = [{"resume": self.auth_token}]
        else:
            params = [
                {
                    "user": {"username": self.username},
                    "password": self.password,
                }
            ]
        result = await self.call("login", params)
        self.user_id = (result or {}).get("id")
        token = (result or {}).get("token")
        if token:
            self.auth_token = token  # resume token for the next reconnect
        if not self.user_id:
            raise ConnectionError("DDP login returned no user id")

    async def _subscribe(self) -> None:
        await self._send(
            {
                "msg": "sub",
                "id": uuid.uuid4().hex,
                "name": "stream-room-messages",
                "params": [MY_MESSAGES, False],
            }
        )

    # ----------------------------------------------------------------- #
    # Wire I/O
    # ----------------------------------------------------------------- #

    async def call(self, method: str, params: list[Any]) -> Any:
        """Invoke a DDP method and wait for its result."""
        if self._ws is None:
            raise ConnectionError("not connected")
        call_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        await self._send(
            {"msg": "method", "method": method, "id": call_id, "params": params}
        )
        try:
            return await asyncio.wait_for(future, timeout=self.call_timeout)
        finally:
            self._pending.pop(call_id, None)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError("not connected")
        await self._ws.send(json.dumps(payload))

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            await self._send({"msg": "ping"})

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for frame in self._ws:
                try:
                    payload = json.loads(frame)
                except ValueError:
                    log.debug("Ignoring non-JSON DDP frame")
                    continue
                await self._dispatch(payload)
        except ConnectionClosed as exc:
            raise ConnectionError(f"websocket closed: {exc}") from exc
        raise ConnectionError("websocket stream ended")

    async def _dispatch(self, payload: dict[str, Any]) -> None:
        kind = payload.get("msg")

        if kind == "ping":
            await self._send({"msg": "pong", **({"id": payload["id"]} if "id" in payload else {})})
            return

        if kind == "connected":
            self._resolve("__connected__", payload)
            return

        if kind == "failed":
            raise ConnectionError(
                f"server requires DDP version {payload.get('version')}"
            )

        if kind == "result":
            call_id = payload.get("id", "")
            if "error" in payload:
                error = payload["error"]
                message = error.get("reason") or error.get("message") or str(error)
                self._reject(call_id, ConnectionError(f"DDP method failed: {message}"))
            else:
                self._resolve(call_id, payload.get("result"))
            return

        if kind == "changed" and payload.get("collection") == "stream-room-messages":
            fields = payload.get("fields") or {}
            message = parse_message(fields.get("args") or [])
            if message and self.on_message is not None:
                await self.on_message(message)

    # ----------------------------------------------------------------- #

    def _resolve(self, call_id: str, value: Any) -> None:
        future = self._pending.pop(call_id, None)
        if future is not None and not future.done():
            future.set_result(value)

    def _reject(self, call_id: str, error: Exception) -> None:
        future = self._pending.pop(call_id, None)
        if future is not None and not future.done():
            future.set_exception(error)

    def _fail_pending(self, reason: str) -> None:
        for call_id in list(self._pending):
            self._reject(call_id, ConnectionError(reason))
