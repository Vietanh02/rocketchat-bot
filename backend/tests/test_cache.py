"""Caches: looked-up passages, integration answers, and kept answers (saved and pinned)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.cache.answers import key_hash
from app.cache.ttl import TTLCache
from app.integrations.executor import IntegrationSpec, execute_integration
from app.knowledge.base import BASE_SCOPE
from app.knowledge.service import KnowledgeService
from app.knowledge.vectorstore import MemoryVectorStore
from app.models import AnswerEntry, AnswerKind
from app.rocketchat.ddp import IncomingMessage
from tests.test_agent import model, settle  # noqa: F401
from tests.test_knowledge import NO_VECTORS, add_base, kb_of, set_knowledge
from tests.test_llm import set_llm  # noqa: F401
from tests.test_memory import FakeEmbeddings, set_embedding
from tests.test_responder import incoming

QUESTION = "How many days of annual leave do I get?"
STANDALONE = "How many days of annual leave does an employee get?"


def agent_of(app_env):
    return app_env.bot.responder.agent


def pick_first(body) -> str:
    return '{"passages": [1]}'


def keep(question: str = STANDALONE, answer: str = "Twelve days per year [1].") -> callable:
    return lambda body: json.dumps({"reusable": True, "question": question, "answer": answer})


async def settled(app_env) -> None:
    """Wait for the reply's after-work and for answers being kept."""
    await settle(app_env)
    background = list(agent_of(app_env)._background)
    if background:
        await asyncio.gather(*background, return_exceptions=True)
    await settle(app_env)


async def ask(app_env, text: str, message_id: str = "M1", **kwargs) -> None:
    await app_env.bot._handle(incoming(text, message_id=message_id, **kwargs))
    await settled(app_env)


def downvote(app_env, message_id: str, by: str = "alice") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        room_id="R1",
        text="",
        sender_id=app_env.bot._bot_user_id,
        sender_username="taskbot",
        room_type="d",
        raw={"_id": message_id, "reactions": {":thumbsdown:": {"usernames": [by]}}},
    )


@pytest.fixture
def bot_user(app_env):
    app_env.bot._bot_user_id = "BOT"
    app_env.bot._bot_username = "taskbot"


# --------------------------------------------------------------------------- #
# The in-memory cache
# --------------------------------------------------------------------------- #


def test_entries_expire_and_the_oldest_go_first():
    now = [0.0]
    cache: TTLCache[str] = TTLCache(2, 10, clock=lambda: now[0])
    cache.put("a", "1")
    cache.put("b", "2")
    assert cache.get("a") == "1"  # now the most recent
    cache.put("c", "3")
    assert cache.get("b") is None and cache.get("a") == "1"
    cache.put("short", "x", ttl=1)
    now[0] = 5
    assert cache.get("short") is None and cache.get("a") == "1"
    now[0] = 20
    assert cache.get("a") is None
    assert cache.pop("c") is None
    assert cache.stats()["hits"] == 3


# --------------------------------------------------------------------------- #
# Looked-up passages
# --------------------------------------------------------------------------- #


async def test_the_same_question_is_looked_up_once(app_env, gateway, model):
    await add_base(app_env)
    model.on_judge = pick_first
    model.answers = ["Twelve [1].", "Twelve [1]."]
    await ask(app_env, QUESTION)
    await ask(app_env, "  how many days of annual LEAVE do I get?", message_id="M2")
    judged = [r for r in model.side_requests if "You check search results" in r["messages"][0]["content"]]
    assert len(judged) == 1
    assert len(model.requests) == 2  # both answered by the model, from the same passages


async def test_a_change_to_the_documents_empties_the_lookups(app_env, gateway, model):
    await add_base(app_env)
    kb = kb_of(app_env)
    await kb.search("annual leave", [BASE_SCOPE], embedding=NO_VECTORS)
    assert len(kb.results) == 1
    before = await kb.base_version()
    await add_base(app_env, "more.md", "# Parking\n\nParking is free for employees.")
    assert len(kb.results) == 0
    assert await kb.base_version() != before


# --------------------------------------------------------------------------- #
# Saved answers
# --------------------------------------------------------------------------- #


async def test_an_answer_from_the_knowledge_base_is_kept_and_given_again(app_env, gateway, model):
    await add_base(app_env)
    model.on_judge = pick_first
    model.on_learn = keep()
    model.answers = ["You get twelve days per year [1]."]
    await ask(app_env, QUESTION)
    [entry] = await agent_of(app_env).answers.entries()
    assert entry.kind == AnswerKind.auto.value and entry.question == STANDALONE
    assert "Annual leave" in entry.sources_json
    # The fast model saw the passages and was asked for the question on its own.
    learn = [r for r in model.side_requests if "You prepare an answer" in r["messages"][0]["content"]][0]
    assert "Every employee gets twelve days" in learn["messages"][1]["content"]

    asked = len(model.requests)
    await ask(app_env, "how many days of annual leave does an employee get", message_id="M2")
    reply = gateway.sent[-1].text
    assert "Twelve days per year [1]." in reply and "Saved answer" in reply
    assert "Sources: [1] handbook" in reply
    assert len(model.requests) == asked  # no model at all
    entry = (await agent_of(app_env).answers.entries())[0]
    assert entry.hits == 1
    # It joins the conversation like any answer.
    state = await agent_of(app_env).sessions.open("dm:R1", "dm", None)
    assert state.messages[-1]["content"] == "Twelve days per year [1]."


async def test_answers_that_suit_only_the_asker_are_not_kept(app_env, gateway, model):
    await add_base(app_env)
    model.on_judge = pick_first
    model.answers = ["Twelve [1]."]
    await ask(app_env, QUESTION)  # the fast model says it is not reusable
    assert await agent_of(app_env).answers.entries() == []
    # An answer that cites nothing is not kept either.
    model.on_learn = keep(answer="Twelve days.")
    model.answers = ["Twelve [1]."]
    await ask(app_env, "How long is annual leave?", message_id="M2")
    assert await agent_of(app_env).answers.entries() == []


async def test_nothing_is_kept_when_the_model_was_unsure_or_used_a_tool(app_env, gateway, model):
    from tests.test_llm import tool_use

    await add_base(app_env)
    model.on_learn = keep()
    model.answers = ["Twelve [1]."]
    await ask(app_env, QUESTION)  # the passages were not judged
    model.on_judge = pick_first
    model.answers = [tool_use("kb_search", query="leave"), "Twelve [1]."]
    await ask(app_env, "What is the annual leave policy?", message_id="M2")
    assert await agent_of(app_env).answers.entries() == []


async def test_a_saved_answer_goes_when_the_knowledge_base_changes(app_env, gateway, model):
    await add_base(app_env)
    model.on_judge = pick_first
    model.on_learn = keep()
    model.answers = ["Twelve [1]."]
    await ask(app_env, QUESTION)
    assert len(await agent_of(app_env).answers.entries()) == 1
    await add_base(app_env, "more.md", "# Leave update\n\nFrom 2027, annual leave is fourteen days.")
    model.answers = ["Fourteen [1]."]
    model.on_learn = keep(answer="Fourteen days [1].")
    await ask(app_env, STANDALONE, message_id="M2")
    assert "Saved answer" not in gateway.sent[-1].text
    # The old answer went; the new one is kept in its place.
    assert [e.answer for e in await agent_of(app_env).answers.entries()] == ["Fourteen days [1]."]


async def test_saved_answers_can_be_turned_off_and_expire(app_env, gateway, model):
    from datetime import timedelta

    from app.models import utcnow

    await add_base(app_env)
    answers = agent_of(app_env).answers
    version = await kb_of(app_env).base_version()
    entry = await answers._add(
        "auto", STANDALONE, "Twelve [1].", [STANDALONE], kb_version=version,
        expires_at=utcnow() + timedelta(hours=1),
    )
    settings = (await app_env.config_store.current()).knowledge
    assert await answers.find(STANDALONE, embedding=NO_VECTORS, settings=settings) is not None
    await set_knowledge(app_env, answer_cache=False)
    off = (await app_env.config_store.current()).knowledge
    assert await answers.find(STANDALONE, embedding=NO_VECTORS, settings=off) is None
    async with app_env.session_factory() as db:
        row = await db.get(AnswerEntry, entry)
        row.expires_at = utcnow() - timedelta(minutes=1)
        await db.commit()
    assert await answers.find(STANDALONE, embedding=NO_VECTORS, settings=settings) is None
    assert await answers.entries() == []  # dropped


async def test_documents_sent_in_the_conversation_come_before_kept_answers(app_env, gateway, model):
    from tests.test_knowledge import with_file

    answers = agent_of(app_env).answers
    await answers.pin(STANDALONE, "Twelve days.", embedding=NO_VECTORS)
    await app_env.bot.responder.respond(with_file("", "notes.txt", b"Leave here is 20 days.", gateway))
    model.answers = ["Twenty."]
    await ask(app_env, STANDALONE, message_id="M2")
    assert "FAQ" not in gateway.sent[-1].text and len(model.requests) == 1


# --------------------------------------------------------------------------- #
# 👎
# --------------------------------------------------------------------------- #


async def test_a_thumbs_down_drops_the_saved_answer_and_asks_again(app_env, gateway, model, bot_user):
    await add_base(app_env)
    answers = agent_of(app_env).answers
    version = await kb_of(app_env).base_version()
    await answers._add("auto", STANDALONE, "Ten days [1].", [STANDALONE], kb_version=version)
    await ask(app_env, STANDALONE)
    kept = gateway.sent[-1]
    assert "Saved answer" in kept.text

    model.answers = ["Twelve days [1]."]
    # The bot's own message comes back with the reaction on it.
    await app_env.bot._enqueue(downvote(app_env, kept.message_id))
    await asyncio.gather(*list(app_env.bot._lane_tasks), return_exceptions=True)
    await settled(app_env)
    assert await answers.entries() == []
    assert gateway.sent[-1].text.startswith("Twelve days [1].")
    assert len(model.requests) == 1  # worked out afresh by the model
    # A second 👎 on the same message does nothing more.
    await app_env.bot._enqueue(downvote(app_env, kept.message_id, by="bo"))
    await asyncio.gather(*list(app_env.bot._lane_tasks), return_exceptions=True)
    assert len(model.requests) == 1


async def test_a_thumbs_down_keeps_a_model_answer_from_being_saved(app_env, gateway, model, bot_user):
    await add_base(app_env)
    model.on_judge = pick_first
    model.on_learn = keep()
    model.answers = ["Twelve [1]."]
    await ask(app_env, QUESTION)
    assert len(await agent_of(app_env).answers.entries()) == 1
    await app_env.bot._enqueue(downvote(app_env, gateway.sent[-1].message_id))
    await asyncio.gather(*list(app_env.bot._lane_tasks), return_exceptions=True)
    assert await agent_of(app_env).answers.entries() == []
    assert len(model.requests) == 1  # not asked again


async def test_the_bots_own_reactions_do_not_count(app_env, gateway, model, bot_user):
    from app.bot.runner import downvoters

    reactions = {":thumbsdown:": {"usernames": ["taskbot", "an"]}, ":eyes:": {"usernames": ["x"]}}
    assert downvoters(reactions, "taskbot") == ["an"]
    assert downvoters({":-1:": {"usernames": ["taskbot"]}}, "taskbot") == []
    assert downvoters(None, "taskbot") == []


# --------------------------------------------------------------------------- #
# Pinned answers
# --------------------------------------------------------------------------- #


async def test_a_pinned_answer_is_given_to_any_way_of_asking_it(app_env, gateway, model):
    answers = agent_of(app_env).answers
    await answers.pin(
        "What is the Wi-Fi password?",
        "Ask IT at extension 2.",
        ["wifi password", "How do I get on the Wi-Fi?"],
        embedding=NO_VECTORS,
    )
    await ask(app_env, "what is the wi-fi password")
    assert "Ask IT at extension 2." in gateway.sent[-1].text and "FAQ" in gateway.sent[-1].text
    await ask(app_env, "How do I get on the Wi-Fi?", message_id="M2")
    assert "Ask IT" in gateway.sent[-1].text
    assert model.requests == []
    # Saved answers being off does not stop pinned ones.
    await set_knowledge(app_env, answer_cache=False)
    await ask(app_env, "WIFI PASSWORD?", message_id="M3")
    assert "Ask IT" in gateway.sent[-1].text
    assert key_hash("Wifi  password?") == key_hash("wifi password")


async def test_kept_answers_are_found_by_meaning(app_env, gateway, model):
    server = FakeEmbeddings()
    store = MemoryVectorStore()
    agent = agent_of(app_env)
    agent.knowledge = KnowledgeService(
        app_env.session_factory,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
        store=store,
    )
    agent.kb.knowledge = agent.knowledge
    agent.answers.knowledge = agent.knowledge
    await set_embedding(app_env, provider="tei", base_url="http://tei.invalid", query_prefix="query: ")
    await set_knowledge(app_env, pinned_min_similarity=0.8)
    embedding = (await app_env.config_store.current()).embedding
    await agent.answers.pin("May I bring my cat?", "Pets stay at home.", embedding=embedding)
    await agent.answers._index_task
    assert await store.count("answer_cache") == 1

    await ask(app_env, "is my cat allowed")
    assert "Pets stay at home." in gateway.sent[-1].text
    model.answers = ["No idea."]
    await ask(app_env, "when is the coffee machine cleaned?", message_id="M2")
    assert gateway.sent[-1].text.startswith("No idea.")


# --------------------------------------------------------------------------- #
# The portal
# --------------------------------------------------------------------------- #


async def test_admins_pin_change_and_remove_answers(admin_client, app_env):
    response = await admin_client.post(
        "/api/cache/answers",
        json={"question": "Where is the office?", "alternatives": ["office address"], "answer": "12 Main St."},
    )
    assert response.status_code == 201, response.text
    entry = response.json()
    assert entry["kind"] == "pinned" and entry["alternatives"] == ["office address"]

    tried = (await admin_client.post("/api/cache/test", json={"question": "OFFICE ADDRESS"})).json()
    assert tried["found"] and tried["entry_id"] == entry["id"] and "12 Main St." in tried["reply"]
    assert (await admin_client.get("/api/cache/answers")).json()[0]["hits"] == 0  # trying is not counting

    changed = await admin_client.put(
        f"/api/cache/answers/{entry['id']}", json={"answer": "14 Main St.", "alternatives": []}
    )
    assert changed.json()["answer"] == "14 Main St." and changed.json()["alternatives"] == []
    assert not (await admin_client.post("/api/cache/test", json={"question": "office address"})).json()["found"]

    stats = (await admin_client.get("/api/cache/stats")).json()
    assert stats["pinned"] == 1 and stats["saved"] == 0
    assert (await admin_client.delete(f"/api/cache/answers/{entry['id']}")).status_code == 204
    assert (await admin_client.get("/api/cache/answers")).json() == []


async def test_a_saved_answer_can_be_kept_or_cleared(admin_client, app_env):
    answers = agent_of(app_env).answers
    first = await answers._add("auto", "Q one?", "A one.", ["Q one?"], kb_version="x")
    await answers._add("auto", "Q two?", "A two.", ["Q two?"], kb_version="x")
    kept = (await admin_client.put(f"/api/cache/answers/{first}", json={})).json()
    assert kept["kind"] == "pinned" and kept["expires_at"] is None
    cleared = (await admin_client.delete("/api/cache/answers")).json()
    assert cleared["removed"] == 1
    assert [e["id"] for e in (await admin_client.get("/api/cache/answers")).json()] == [first]


async def test_cache_settings_are_checked(admin_client):
    body = (await admin_client.put("/api/cache/config", json={"answer_min_similarity": 0.97})).json()
    assert body["answer_min_similarity"] == 0.97 and body["answer_cache"] is True
    response = await admin_client.put("/api/cache/config", json={"answer_ttl_hours": 0})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Integration answers
# --------------------------------------------------------------------------- #


async def test_an_integration_answer_is_reused_for_the_same_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"temp": 31})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    spec = IntegrationSpec(
        id=1,
        name="weather",
        description="",
        base_url="https://api.example.com",
        method="GET",
        path_template="/w/{{ params.city }}",
        reply_template="{{ response.temp }}°C for {{ sender }}",
        cache_seconds=60,
    )
    cache: TTLCache = TTLCache(10, 60)
    first = await execute_integration(
        spec, params={"city": "hanoi"}, base_context={"sender": "an"}, client=client, cache=cache
    )
    again = await execute_integration(
        spec, params={"city": "hanoi"}, base_context={"sender": "bo"}, client=client, cache=cache
    )
    other = await execute_integration(
        spec, params={"city": "hue"}, base_context={"sender": "bo"}, client=client, cache=cache
    )
    assert len(calls) == 2 and again.cached and not first.cached and not other.cached
    # The reply is shown for who asks this time.
    assert first.text == "31°C for an" and again.text == "31°C for bo"
    # Without cache_seconds nothing is kept.
    spec.cache_seconds = 0
    await execute_integration(spec, params={"city": "hanoi"}, base_context={}, client=client, cache=cache)
    assert len(calls) == 3
