"""Thin async wrapper over the Rocket.Chat REST API.

Everything the bot *does* goes through here. Only inbound message streaming
uses the realtime/DDP API (see ddp.py), which keeps our exposure to the
realtime API deprecation down to one module.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class RocketChatError(RuntimeError):
    """A REST call failed or Rocket.Chat reported success=false."""


class RocketChatRest:
    def __init__(
        self,
        base_url: str,
        user_id: str | None = None,
        auth_token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.auth_token = auth_token
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v1", timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----------------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        if not (self.user_id and self.auth_token):
            return {}
        return {"X-User-Id": self.user_id, "X-Auth-Token": self.auth_token}

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method, path, json=json, params=params, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise RocketChatError(f"{method} {path} failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            raise RocketChatError(
                f"{method} {path} returned non-JSON (HTTP {response.status_code})"
            ) from None

        if response.status_code >= 400 or payload.get("success") is False:
            detail = payload.get("error") or payload.get("message") or response.text
            raise RocketChatError(f"{method} {path} -> HTTP {response.status_code}: {detail}")
        return payload

    # ----------------------------------------------------------------- #
    # Auth
    # ----------------------------------------------------------------- #

    async def login(self, username: str, password: str) -> tuple[str, str]:
        """Exchange credentials for (user_id, auth_token) and keep them."""
        payload = await self._request(
            "POST", "/login", json={"user": username, "password": password}
        )
        data = payload.get("data", {})
        user_id, token = data.get("userId"), data.get("authToken")
        if not (user_id and token):
            raise RocketChatError("login succeeded but returned no credentials")
        self.user_id, self.auth_token = user_id, token
        return user_id, token

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    # ----------------------------------------------------------------- #
    # Messaging
    # ----------------------------------------------------------------- #

    async def post_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        alias: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"roomId": room_id, "text": text}
        if thread_id:
            body["tmid"] = thread_id
        if alias:
            body["alias"] = alias
        if attachments:
            body["attachments"] = attachments
        return await self._request("POST", "/chat.postMessage", json=body)

    async def update_message(self, room_id: str, message_id: str, text: str) -> dict[str, Any]:
        """Replace the text of a message the bot already posted."""
        return await self._request(
            "POST",
            "/chat.update",
            json={"roomId": room_id, "msgId": message_id, "text": text},
        )

    # ----------------------------------------------------------------- #
    # Rooms & users
    # ----------------------------------------------------------------- #

    async def create_dm(self, username: str) -> str:
        """Open (or reuse) a direct message room with a user; returns room id."""
        payload = await self._request("POST", "/im.create", json={"username": username})
        room_id = (payload.get("room") or {}).get("_id")
        if not room_id:
            raise RocketChatError(f"im.create for {username!r} returned no room id")
        return room_id

    async def room_info(self, room_id: str) -> dict[str, Any]:
        return await self._request("GET", "/rooms.info", params={"roomId": room_id})

    async def user_info(self, *, user_id: str | None = None, username: str | None = None) -> dict[str, Any]:
        params = {"userId": user_id} if user_id else {"username": username}
        return await self._request("GET", "/users.info", params=params)

    async def channel_id_by_name(self, name: str) -> str:
        payload = await self._request(
            "GET", "/channels.info", params={"roomName": name.lstrip("#")}
        )
        room_id = (payload.get("channel") or {}).get("_id")
        if not room_id:
            raise RocketChatError(f"no channel named {name!r}")
        return room_id
