"""DDP frame parsing and dispatch, without a Rocket.Chat server."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.rocketchat.ddp import MY_MESSAGES, DDPClient, parse_message


def test_parses_a_message_event():
    message = parse_message(
        [
            {"_id": "M1", "rid": "R1", "msg": "hello", "u": {"_id": "U1", "username": "bo"}},
            {"roomType": "d"},
        ]
    )
    assert (message.message_id, message.room_id, message.text) == ("M1", "R1", "hello")
    assert message.sender_username == "bo" and message.room_type == "d"
    assert not (message.is_system or message.is_edit or message.is_bot)


@pytest.mark.parametrize("args", [[], [None], [{"no_rid": 1}], ["not a dict"]])
def test_malformed_payloads_are_ignored(args):
    assert parse_message(args) is None


def test_system_edited_and_bot_messages_are_flagged():
    assert parse_message([{"rid": "R", "t": "uj"}]).is_system
    assert parse_message([{"rid": "R", "editedAt": "2026-01-01"}]).is_edit
    assert parse_message([{"rid": "R", "bot": {"i": "x"}}]).is_bot
    assert parse_message([{"rid": "R", "tmid": "T1"}]).thread_id == "T1"


def test_credentials_are_required():
    with pytest.raises(ValueError):
        DDPClient("ws://localhost:3000/websocket")


class FakeSocket:
    """Records what the client sends, so the handshake can be asserted on."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


async def client_with_socket() -> tuple[DDPClient, FakeSocket]:
    socket = FakeSocket()
    client = DDPClient("ws://localhost:3000/websocket", auth_token="tok")
    client._ws = socket
    return client, socket


async def test_a_server_ping_is_answered_with_a_pong():
    client, socket = await client_with_socket()
    await client._dispatch({"msg": "ping", "id": "p1"})
    assert socket.sent == [{"msg": "pong", "id": "p1"}]


async def test_a_ping_without_an_id_is_answered_without_one():
    client, socket = await client_with_socket()
    await client._dispatch({"msg": "ping"})
    assert socket.sent == [{"msg": "pong"}]


async def test_the_subscription_uses_the_all_messages_pseudo_room():
    client, socket = await client_with_socket()
    await client._subscribe()
    sub = socket.sent[0]
    assert sub["name"] == "stream-room-messages"
    assert sub["params"] == [MY_MESSAGES, False]


async def test_a_method_result_resolves_the_caller():
    client, socket = await client_with_socket()
    call = asyncio.create_task(client.call("login", [{"resume": "tok"}]))
    await asyncio.sleep(0)  # let the call register its future and send the frame

    call_id = socket.sent[0]["id"]
    await client._dispatch({"msg": "result", "id": call_id, "result": {"id": "U9"}})
    assert await call == {"id": "U9"}


async def test_a_method_error_raises_for_the_caller():
    client, socket = await client_with_socket()
    call = asyncio.create_task(client.call("login", [{}]))
    await asyncio.sleep(0)

    call_id = socket.sent[0]["id"]
    await client._dispatch(
        {"msg": "result", "id": call_id, "error": {"reason": "Unauthorized"}}
    )
    with pytest.raises(ConnectionError, match="Unauthorized"):
        await call


async def test_a_version_mismatch_ends_the_session():
    client, _ = await client_with_socket()
    with pytest.raises(ConnectionError, match="DDP version"):
        await client._dispatch({"msg": "failed", "version": "2"})


async def test_stream_events_reach_the_handler():
    received = []
    client, _ = await client_with_socket()
    client.on_message = received.append  # type: ignore[assignment]

    async def collect(message):
        received.append(message)

    client.on_message = collect
    await client._dispatch(
        {
            "msg": "changed",
            "collection": "stream-room-messages",
            "fields": {
                "eventName": MY_MESSAGES,
                "args": [{"_id": "M1", "rid": "R1", "msg": "hi", "u": {"username": "bo"}}],
            },
        }
    )
    assert len(received) == 1 and received[0].text == "hi"


async def test_other_collections_are_ignored():
    client, _ = await client_with_socket()

    async def explode(_message):
        raise AssertionError("should not be called")

    client.on_message = explode
    await client._dispatch({"msg": "changed", "collection": "stream-notify-user", "fields": {}})


async def test_pending_calls_fail_when_the_connection_drops():
    client, socket = await client_with_socket()
    call = asyncio.create_task(client.call("login", [{}]))
    await asyncio.sleep(0)

    client._fail_pending("connection closed")
    with pytest.raises(ConnectionError, match="connection closed"):
        await call
