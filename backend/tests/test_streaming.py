"""Streamed replies: the message grows in place, and always ends complete."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.bot import streaming
from app.bot.streaming import (
    MAX_STREAM_EDITS,
    STREAM_CURSOR,
    DeliveryOptions,
    LiveReply,
    deliver,
    render_partial,
    stream_steps,
)
from app.models import BotConfig, MessageLog
from app.rocketchat.gateway import FakeGateway
from app.rocketchat.ddp import IncomingMessage
from app.rocketchat.rest import RocketChatError

STREAM = DeliveryOptions(mode="stream", chunk_chars=10, interval_ms=50)
LONG = "The quick brown fox jumps over the lazy dog, then naps in the sun."


def incoming(text: str) -> IncomingMessage:
    return IncomingMessage(
        message_id="M1",
        room_id="R1",
        text=text,
        sender_id="U1",
        sender_username="bo",
        room_type="d",
    )


async def no_sleep(_: float) -> None:
    return None


# --------------------------------------------------------------------------- #
# Splitting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("chunk", [1, 5, 10, 33, 500])
def test_steps_grow_and_end_with_the_full_text(chunk):
    steps = stream_steps(LONG, chunk, max_steps=100)
    assert steps[-1] == LONG
    for shorter, longer in zip(steps, steps[1:]):
        assert longer.startswith(shorter) and len(longer) > len(shorter)


def test_cuts_fall_between_words():
    for step in stream_steps(LONG, 10, max_steps=100)[:-1]:
        assert step[-1].isspace()


def test_a_word_longer_than_a_chunk_is_split():
    assert stream_steps("abcdefghij", 4, max_steps=100) == ["abcd", "abcdefgh", "abcdefghij"]


def test_every_step_shows_something_new():
    steps = stream_steps("one\n\n  two six", 4, max_steps=100)
    assert steps == ["one\n", "one\n\n  two ", "one\n\n  two six"]


def test_a_split_never_strands_a_combining_mark():
    text = "Việt"  # "Việt" written with combining marks
    for step in stream_steps(text, 3, max_steps=100):
        assert not step.endswith("e")


def test_long_text_uses_bigger_chunks_not_more_steps():
    text = "word " * 2000
    assert len(stream_steps(text, 5, max_steps=10)) <= 10


def test_short_text_is_a_single_step():
    assert stream_steps("hi", 24, max_steps=10) == ["hi"]


def test_partial_text_gets_a_cursor_and_a_closed_code_fence():
    assert render_partial("hello ") == "hello" + STREAM_CURSOR
    shown = render_partial("see:\n```\nprint(1)")
    assert shown.endswith("```" + STREAM_CURSOR)
    assert shown.count("```") == 2


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


async def test_whole_mode_posts_once_and_never_edits():
    gateway = FakeGateway()
    await deliver(gateway, "R1", LONG, options=DeliveryOptions(), sleep=no_sleep)
    assert [m.text for m in gateway.sent] == [LONG]
    assert gateway.updates == []


async def test_stream_mode_posts_once_then_edits_up_to_the_full_text():
    gateway = FakeGateway()
    sent = await deliver(gateway, "R1", LONG, thread_id="T1", options=STREAM, sleep=no_sleep)

    assert len(gateway.sent) == 1
    assert gateway.sent[0].thread_id == "T1"
    assert gateway.sent[0].text == LONG
    assert sent.text == LONG and sent.message_id == gateway.sent[0].message_id

    texts = [text for _, text in gateway.updates]
    assert len(texts) >= 3
    assert all(text.endswith(STREAM_CURSOR) for text in texts[:-1])
    assert texts[-1] == LONG


async def test_stream_mode_waits_the_configured_interval_between_edits():
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    gateway = FakeGateway()
    await deliver(gateway, "R1", LONG, options=STREAM, sleep=record)
    assert waits and set(waits) == {0.05}
    assert len(waits) == len(gateway.updates)


async def test_the_number_of_edits_is_capped():
    gateway = FakeGateway()
    options = DeliveryOptions(mode="stream", chunk_chars=1, interval_ms=50)
    await deliver(gateway, "R1", "x " * 5000, options=options, sleep=no_sleep)
    assert len(gateway.updates) <= MAX_STREAM_EDITS


async def test_a_short_reply_is_not_streamed():
    gateway = FakeGateway()
    await deliver(gateway, "R1", "ok", options=STREAM, sleep=no_sleep)
    assert [m.text for m in gateway.sent] == ["ok"] and gateway.updates == []


async def test_if_editing_is_refused_the_full_reply_is_still_sent():
    gateway = FakeGateway()
    gateway.fail_updates = RocketChatError("editing is not allowed")
    sent = await deliver(gateway, "R1", LONG, options=STREAM, sleep=no_sleep)

    assert len(gateway.sent) == 2
    assert gateway.sent[0].text.endswith(STREAM_CURSOR)
    assert gateway.sent[1].text == LONG == sent.text


# --------------------------------------------------------------------------- #
# Live replies (output shown while it is produced)
# --------------------------------------------------------------------------- #


@pytest.fixture
def fast_live(monkeypatch):
    monkeypatch.setattr(streaming, "LIVE_MIN_INTERVAL_SECONDS", 0.05)
    return DeliveryOptions(mode="stream", interval_ms=50)


async def test_a_live_reply_posts_at_once_then_edits_the_same_message(fast_live):
    gateway = FakeGateway()
    live = LiveReply(gateway, "R1", thread_id="T1", options=fast_live)

    live.push("step 1\n")
    await asyncio.sleep(0.01)
    assert [m.text for m in gateway.sent] == ["step 1" + STREAM_CURSOR]
    assert gateway.sent[0].thread_id == "T1"

    live.push("step 1\nstep 2\n")
    await asyncio.sleep(0.1)
    assert gateway.updates[-1][1] == "step 1\nstep 2" + STREAM_CURSOR

    sent = await live.finish("step 1\nstep 2")
    assert [m.text for m in gateway.sent] == ["step 1\nstep 2"]
    assert sent.message_id == gateway.sent[0].message_id


async def test_pushes_within_one_interval_become_one_edit(fast_live):
    gateway = FakeGateway()
    live = LiveReply(gateway, "R1", options=fast_live)
    live.push("a")
    await asyncio.sleep(0.01)
    for text in ("a b", "a b c", "a b c d"):
        live.push(text)
    await asyncio.sleep(0.1)
    assert [text for _, text in gateway.updates] == ["a b c d" + STREAM_CURSOR]
    await live.finish("a b c d")


async def test_the_portal_interval_cannot_go_below_the_live_minimum(fast_live):
    gateway = FakeGateway()
    live = LiveReply(gateway, "R1", options=DeliveryOptions(mode="stream", interval_ms=0))
    live.push("a")
    await asyncio.sleep(0.01)
    live.push("a b")
    await asyncio.sleep(0.02)
    assert gateway.updates == []  # still inside the 50 ms minimum
    await live.finish("a b")
    assert gateway.last_text == "a b"


async def test_printed_output_is_typed_out_by_characters_per_update(fast_live):
    gateway = FakeGateway()
    live = LiveReply(gateway, "R1", options=DeliveryOptions(mode="stream", chunk_chars=4,
                                                            interval_ms=50))
    live.push("one two six\n")  # printed in one go
    await asyncio.sleep(0.01)
    assert [m.text for m in gateway.sent] == ["one" + STREAM_CURSOR]
    await asyncio.sleep(0.2)
    assert [text for _, text in gateway.updates] == [
        "one two" + STREAM_CURSOR,
        "one two six" + STREAM_CURSOR,
    ]
    await live.finish("one two six")
    assert gateway.last_text == "one two six"


async def test_printed_output_is_typed_out_at_the_configured_delay(fast_live):
    gateway = FakeGateway()
    live = LiveReply(gateway, "R1", options=DeliveryOptions(mode="stream", chunk_chars=2,
                                                            interval_ms=200))
    live.push("a b c")
    await asyncio.sleep(0.1)
    assert gateway.updates == []  # still inside the 200 ms delay
    await asyncio.sleep(0.2)
    assert [text for _, text in gateway.updates] == ["a b" + STREAM_CURSOR]
    live.abort()


async def test_the_rest_is_typed_out_after_the_script_exits(fast_live):
    gateway = FakeGateway()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(seconds)

    live = LiveReply(gateway, "R1", sleep=sleep,
                     options=DeliveryOptions(mode="stream", chunk_chars=4, interval_ms=50))
    live.push("one")
    await asyncio.sleep(0.01)
    sent = await live.finish("one two six")
    assert [text for _, text in gateway.updates] == [
        "one two" + STREAM_CURSOR,
        "one two six",
    ]
    assert len(slept) == 2 and all(0 < s <= 0.05 for s in slept)
    assert sent.message_id == gateway.sent[0].message_id


async def test_finishing_keeps_to_the_streaming_time_limit(fast_live):
    gateway = FakeGateway()
    live = LiveReply(gateway, "R1", sleep=no_sleep,
                     options=DeliveryOptions(mode="stream", chunk_chars=1, interval_ms=50))
    live.push("x")
    await asyncio.sleep(0.01)
    await live.finish("x " + LONG * 20)
    assert len(gateway.updates) <= MAX_STREAM_EDITS
    assert gateway.last_text == "x " + LONG * 20


async def test_output_that_was_never_posted_is_streamed_as_a_normal_reply(fast_live):
    gateway = FakeGateway()
    live = LiveReply(gateway, "R1", sleep=no_sleep, options=STREAM)
    await live.finish(LONG)
    assert len(gateway.sent) == 1 and len(gateway.updates) > 1
    assert gateway.updates[0][1] == render_partial(stream_steps(LONG, 10, 41)[1])
    assert gateway.last_text == LONG


async def test_without_output_the_final_reply_is_simply_sent(fast_live):
    gateway = FakeGateway()
    live = LiveReply(gateway, "R1", options=fast_live)
    live.push("  \n")
    await live.finish(":white_check_mark: Done.")
    assert [m.text for m in gateway.sent] == [":white_check_mark: Done."]
    assert gateway.updates == []


async def test_if_live_edits_are_refused_the_full_reply_follows(fast_live):
    gateway = FakeGateway()
    live = LiveReply(gateway, "R1", options=fast_live)
    live.push("step 1")
    await asyncio.sleep(0.01)
    gateway.fail_updates = RocketChatError("editing is not allowed")
    live.push("step 1 step 2")
    await asyncio.sleep(0.1)
    await live.finish("step 1 step 2")
    assert [m.text for m in gateway.sent] == ["step 1" + STREAM_CURSOR, "step 1 step 2"]


# --------------------------------------------------------------------------- #
# Through the bot
# --------------------------------------------------------------------------- #


async def set_reply_mode(runtime, mode: str) -> None:
    async with runtime.session_factory() as session:
        config = await session.get(BotConfig, 1)
        config.reply_mode = mode
        config.stream_chunk_chars = 10
        config.stream_interval_ms = 0
        config.config_version += 1
        await session.commit()
    runtime.config_store.invalidate()


async def test_the_bot_streams_replies_when_configured(app_env, gateway):
    await set_reply_mode(app_env, "stream")
    await app_env.bot._handle(incoming("help"))

    assert len(gateway.sent) == 1
    assert len(gateway.updates) >= 2
    assert "What I can do" in gateway.last_text
    assert not gateway.last_text.endswith(STREAM_CURSOR)

    async with app_env.session_factory() as session:
        log = (await session.scalars(select(MessageLog))).one()
    assert log.status == "ok" and log.text_out == gateway.last_text


async def test_the_bot_sends_whole_replies_by_default(app_env, gateway):
    await app_env.bot._handle(incoming("help"))
    assert len(gateway.sent) == 1 and gateway.updates == []


# --------------------------------------------------------------------------- #
# Portal API
# --------------------------------------------------------------------------- #


async def test_reply_mode_is_configurable_from_the_portal(admin_client):
    assert (await admin_client.get("/api/config")).json()["reply_mode"] == "whole"

    response = await admin_client.put(
        "/api/config",
        json={"reply_mode": "stream", "stream_chunk_chars": 40, "stream_interval_ms": 300},
    )
    body = response.json()
    assert response.status_code == 200
    assert (body["reply_mode"], body["stream_chunk_chars"], body["stream_interval_ms"]) == (
        "stream",
        40,
        300,
    )


@pytest.mark.parametrize(
    "payload",
    [{"reply_mode": "typewriter"}, {"stream_chunk_chars": 0}, {"stream_interval_ms": 10}],
)
async def test_bad_streaming_settings_are_rejected(admin_client, payload):
    response = await admin_client.put("/api/config", json=payload)
    assert response.status_code == 422
