"""The assistant core: conversations, the per-conversation queue, deadlines,
tools run side by side, the acknowledgement reaction and the timings."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.agent.queue import MERGED, QueueFull, SessionQueue
from app.agent.timing import TurnTimer
from app.api.agent import percentiles
from app.bot.config_store import ensure_agent_config
from app.bot.llm import LLMService
from app.bot.splitting import MAX_MESSAGE_CHARS, split_message
from app.bot.streaming import LiveReply, deliver
from app.models import AgentMessage, AgentRun, AgentSession, BotConfig
from tests.test_llm import FakeModel, fake_transport, set_config, set_llm, tool_use, weather_api  # noqa: F401
from tests.test_responder import context, incoming


async def set_agent(runtime, **fields):
    async with runtime.session_factory() as session:
        config = await ensure_agent_config(session)
        for name, value in fields.items():
            setattr(config, name, value)
        (await session.get(BotConfig, 1)).config_version += 1
        await session.commit()
    runtime.config_store.invalidate()


@pytest.fixture
async def model(app_env):
    fake = FakeModel()
    app_env.bot.responder.llm = LLMService(http_client=fake_transport(fake))
    await set_llm(
        app_env, base_url="http://llm.invalid/v1", api_key="sk-test", model="glm-test",
        history_messages=10,
    )
    await set_config(app_env, responder_mode="llm", bot_username="taskbot")
    return fake


async def runs(app_env) -> list[AgentRun]:
    await settle(app_env)
    async with app_env.session_factory() as db:
        return list((await db.scalars(select(AgentRun).order_by(AgentRun.id))).all())


async def settle(app_env) -> None:
    """Let the work done after a reply (reaction removal, timings) finish."""
    for _ in range(3):
        background = list(app_env.bot.responder._background)
        if not background:
            return
        await asyncio.gather(*background, return_exceptions=True)


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #


async def test_turns_of_one_conversation_run_one_at_a_time():
    queue, order, running = SessionQueue(), [], 0

    async def handle(items):
        nonlocal running
        running += 1
        assert running == 1
        await asyncio.sleep(0.01)
        order.append(items)
        running -= 1
        return items[-1]

    results = await asyncio.gather(*(queue.submit("k", n, handle) for n in range(3)))
    assert results == [0, 1, 2] and order == [[0], [1], [2]]
    assert not queue.busy("k")


async def test_other_conversations_do_not_wait():
    queue, gate = SessionQueue(), asyncio.Event()

    async def slow(items):
        await gate.wait()
        return "slow"

    async def fast(items):
        return "fast"

    blocked = asyncio.ensure_future(queue.submit("a", 1, slow))
    assert await asyncio.wait_for(queue.submit("b", 1, fast), 1) == "fast"
    gate.set()
    assert await blocked == "slow"


async def test_collect_answers_waiting_messages_together():
    queue, gate, seen = SessionQueue(), asyncio.Event(), []

    async def handle(items):
        seen.append(items)
        if items == ["first"]:
            await gate.wait()
        return "+".join(items)

    first = asyncio.ensure_future(queue.submit("k", "first", handle, collect=True))
    await asyncio.sleep(0)
    later = [asyncio.ensure_future(queue.submit("k", t, handle, collect=True)) for t in "ab"]
    await asyncio.sleep(0)
    gate.set()
    assert await first == "first"
    assert [await t for t in later] == [MERGED, "a+b"]
    assert seen == [["first"], ["a", "b"]]


async def test_a_full_conversation_refuses_more():
    queue, gate = SessionQueue(max_pending=1), asyncio.Event()

    async def handle(items):
        await gate.wait()

    running = asyncio.ensure_future(queue.submit("k", 1, handle))
    await asyncio.sleep(0)  # now running; nothing is pending
    waiting = asyncio.ensure_future(queue.submit("k", 2, handle))
    await asyncio.sleep(0)
    with pytest.raises(QueueFull):
        await queue.submit("k", 3, handle)
    gate.set()
    await asyncio.gather(running, waiting)


async def test_a_failing_turn_does_not_block_the_next():
    queue = SessionQueue()

    async def handle(items):
        if items == [1]:
            raise RuntimeError("boom")
        return "ok"

    first = asyncio.ensure_future(queue.submit("k", 1, handle))
    second = asyncio.ensure_future(queue.submit("k", 2, handle))
    with pytest.raises(RuntimeError):
        await first
    assert await second == "ok"


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #


async def test_the_transcript_is_stored(app_env, gateway, model):
    model.answers = ["Hi An"]
    await app_env.bot.responder.respond(context("I am An"))
    async with app_env.session_factory() as db:
        session = await db.scalar(select(AgentSession))
        messages = (await db.scalars(select(AgentMessage).order_by(AgentMessage.id))).all()
    assert session.key == "dm:R1" and session.kind == "dm" and session.owner_username == "bo"
    assert [(m.role, m.content) for m in messages] == [("user", "I am An"), ("assistant", "Hi An")]


async def test_a_thread_is_its_own_conversation(app_env, gateway, model):
    await app_env.bot.responder.respond(context("in the room"))
    await app_env.bot.responder.respond(context("in a thread", thread_id="T1"))
    assert [m["content"] for m in model.last_messages[1:]] == ["in a thread"]


async def test_new_starts_afresh(app_env, gateway, model):
    model.answers = ["Noted.", "Started", "Who?"]
    await app_env.bot.responder.respond(context("my name is An"))
    reply = await app_env.bot.responder.respond(context("new"))
    assert "new conversation" in reply.text
    await app_env.bot.responder.respond(context("what is my name?"))
    assert [m["content"] for m in model.last_messages[1:]] == ["what is my name?"]


async def test_an_idle_conversation_starts_afresh(app_env, gateway, model):
    await set_agent(app_env, idle_reset_minutes=5)
    await app_env.bot.responder.respond(context("first"))
    async with app_env.session_factory() as db:
        session = await db.scalar(select(AgentSession))
        session.last_active_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await db.commit()
    store = app_env.bot.responder.agent.sessions
    store._last_active["dm:R1"] -= timedelta(minutes=10)
    await app_env.bot.responder.respond(context("second"))
    assert [m["content"] for m in model.last_messages[1:]] == ["second"]


async def test_old_messages_are_folded_into_a_summary(app_env, gateway, model):
    await set_agent(app_env, summarize_after_messages=4, fast_model="glm-fast")
    await set_llm(app_env, history_messages=2)
    model.answers = ["a1", "a2", "a3", "An likes tea."]
    for question in ("q1", "q2", "q3"):
        await app_env.bot.responder.respond(context(question))
    agent = app_env.bot.responder.agent
    await asyncio.gather(*agent._background)

    summary_request = model.requests[3]
    assert summary_request["model"] == "glm-fast"
    assert "q1" in summary_request["messages"][1]["content"]

    model.answers = ["ok"]
    await app_env.bot.responder.respond(context("q4"))
    system = model.last_messages[0]["content"]
    assert "An likes tea." in system
    assert [m["content"] for m in model.last_messages[1:]] == ["q3", "a3", "q4"]
    async with app_env.session_factory() as db:
        folded = await db.scalar(
            select(func.count()).select_from(AgentMessage).where(AgentMessage.compacted.is_(True))
        )
    # The latest four (MIN_KEPT_AFTER_SUMMARY) always stay as messages.
    assert folded == 2


# --------------------------------------------------------------------------- #
# Deadline, tools, reaction, timings
# --------------------------------------------------------------------------- #


async def test_a_turn_stops_at_the_deadline(app_env, gateway, model):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(500)

    app_env.bot.responder.llm = LLMService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow))
    )
    await set_agent(app_env, turn_deadline_seconds=0.2)
    await set_llm(app_env, model="glm-slow")  # rebuild the client
    reply = await app_env.bot.responder.respond(context("hi"))
    assert reply.error and "stopped after" in reply.error
    await reply.on_sent(True)
    (run,) = await runs(app_env)
    assert run.status == "timeout"


async def test_tools_of_one_round_run_side_by_side(app_env, gateway, model, monkeypatch):
    from app.agent.tools import ToolRegistry

    active = peak = 0

    async def call(self, call):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return json.dumps({"ok": call.name})

    monkeypatch.setattr(ToolRegistry, "call", call)
    from app.bot.llm import LLMChat

    chat = LLMChat(
        base_url="http://llm.invalid/v1", api_key="k", model="m",
        http_client=fake_transport(model),
    )

    class TwoTools:
        def tools(self):
            return {n: {"description": n, "parameters": {"type": "object"}} for n in ("a", "b")}

        async def call(self, name, arguments):
            raise AssertionError

    def two_calls(request):
        body = json.loads(request.content)
        model.requests.append(body)
        if len(model.requests) == 1:
            calls = [
                {"id": f"c{n}", "type": "function", "function": {"name": n, "arguments": "{}"}}
                for n in ("a", "b")
            ]
            message = {"role": "assistant", "content": None, "tool_calls": calls}
        else:
            message = {"role": "assistant", "content": "done"}
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        })

    chat._client._client = httpx.AsyncClient(transport=httpx.MockTransport(two_calls))
    timer = TurnTimer()
    answer = await chat.complete(
        [{"role": "user", "content": "go"}], tools=ToolRegistry([TwoTools()]), timer=timer
    )
    assert answer == "done" and peak == 2
    assert [t.name for t in timer.tools] == ["a", "b"] and timer.rounds == 2
    assert timer.waits["tools"] < 95  # ran together: ~50ms, not ~100ms


async def test_the_bot_reacts_while_it_thinks(app_env, gateway, model):
    model.answers = ["Hello"]
    await app_env.bot._handle(incoming("hi there", message_id="M9"))
    await settle(app_env)
    assert gateway.last_text == "Hello"
    # Put on at once, taken off once answered.
    assert gateway.reactions == []
    (run,) = await runs(app_env)
    assert run.status == "ok" and run.ack_ms is not None and run.total_ms is not None
    timings = json.loads(run.timings_json)
    assert {"queued", "session_loaded", "llm_done", "delivered"} <= set(timings["marks"])
    assert run.overhead_ms is not None and run.overhead_ms >= 0


async def test_no_reaction_when_turned_off(app_env, gateway, model):
    await set_agent(app_env, ack_reaction="")
    seen = []
    original = gateway.react

    async def react(*args, **kwargs):
        seen.append(args)
        await original(*args, **kwargs)

    gateway.react = react
    await app_env.bot._handle(incoming("hi"))
    await settle(app_env)
    assert seen == []


# --------------------------------------------------------------------------- #
# Long replies
# --------------------------------------------------------------------------- #


def test_long_text_is_split_between_paragraphs():
    paragraph = "word " * 300
    text = "\n\n".join([paragraph.strip()] * 8)
    parts = split_message(text)
    assert len(parts) > 1 and all(len(p) <= MAX_MESSAGE_CHARS for p in parts)
    assert "\n\n".join(parts) == text


def test_a_split_code_block_is_reopened():
    code = "```python\n" + "\n".join(f"x = {n}" for n in range(1500)) + "\n```"
    parts = split_message(code)
    assert len(parts) > 1
    for part in parts:
        assert part.count("```") % 2 == 0 and len(part) <= MAX_MESSAGE_CHARS
    assert parts[1].startswith("```python")


async def test_deliver_posts_a_long_reply_as_several_messages(gateway):
    text = "\n\n".join(["paragraph " * 100] * 12)
    await deliver(gateway, "R1", text)
    assert len(gateway.sent) > 1
    assert all(len(m.text) <= 5000 for m in gateway.sent)


async def test_a_live_reply_moves_to_a_new_message_when_full(gateway):
    async def no_sleep(_):
        return None

    live = LiveReply(gateway, "R1", max_chars=100, sleep=no_sleep)
    text = ""
    for n in range(30):
        text += f"line {n}\n"
        live.push(text)
        await asyncio.sleep(0)
    await live.finish(text)
    assert len(gateway.sent) > 1
    assert all(len(m.text) <= 100 for m in gateway.sent)
    assert "line 29" in gateway.sent[-1].text and "line 0" in gateway.sent[0].text


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def test_percentiles():
    assert percentiles([]).p50 is None
    values = [float(n) for n in range(1, 101)]
    result = percentiles(values)
    assert (result.p50, result.p95, result.max) == (50.0, 95.0, 100.0)


async def test_the_settings_can_be_changed_by_an_admin(admin_client):
    response = await admin_client.get("/api/agent")
    assert response.status_code == 200 and response.json()["ack_reaction"] == ":eyes:"
    response = await admin_client.put(
        "/api/agent", json={"queue_mode": "collect", "turn_deadline_seconds": 30, "ack_reaction": ""}
    )
    assert response.status_code == 200
    assert response.json()["queue_mode"] == "collect" and response.json()["ack_reaction"] == ""
    assert (await admin_client.put("/api/agent", json={"ack_reaction": "eyes"})).status_code == 422


async def test_viewers_cannot_change_the_settings(client, app_env):
    from app.models import PortalUser, Role
    from app.security import hash_password

    async with app_env.session_factory() as session:
        session.add(PortalUser(email="v@example.com", password_hash=hash_password("pw-123456"), role=Role.viewer.value))
        await session.commit()
    login = await client.post("/api/auth/login", json={"email": "v@example.com", "password": "pw-123456"})
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    assert (await client.get("/api/agent/stats")).status_code == 200
    assert (await client.put("/api/agent", json={"queue_mode": "collect"})).status_code == 403


async def test_stats_summarize_recent_turns(admin_client, app_env):
    async with app_env.session_factory() as db:
        for n in range(1, 11):
            db.add(AgentRun(status="ok", ack_ms=n, ttft_ms=n * 10, total_ms=n * 100, overhead_ms=n / 10))
        db.add(AgentRun(status="merged", total_ms=99999))
        await db.commit()
    stats = (await admin_client.get("/api/agent/stats")).json()
    assert stats["runs"] == 10
    assert stats["total_ms"]["p50"] == 500 and stats["total_ms"]["max"] == 1000
    assert len(stats["recent"]) == 10


async def test_a_followed_reply_is_formatted_once_per_edit_and_ends_at_once(gateway):
    from app.bot.streaming import DeliveryOptions

    options = DeliveryOptions(mode="stream", chunk_chars=2, interval_ms=50)
    live = LiveReply(gateway, "R1", options=options, follow=True)
    calls = 0
    text = ""

    def render():
        nonlocal calls
        calls += 1
        return text

    for n in range(200):
        text += f"w{n} "
        live.push(render)
        await asyncio.sleep(0.001)
    started = asyncio.get_running_loop().time()
    await live.finish(text.strip())
    # Not typed out two characters at a time: one last edit.
    assert asyncio.get_running_loop().time() - started < 0.1
    assert gateway.last_text == text.strip()
    assert calls < 50  # once per edit, not once per token
    # Each edit showed everything pushed by then.
    assert all(not t.startswith("w0 w1 ") or len(t) > 10 for _, t in gateway.updates)
