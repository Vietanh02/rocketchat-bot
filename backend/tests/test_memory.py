"""Vector search and the assistant's memory: embeddings and their cache, the
Qdrant client, hybrid search, scopes, the memory and profile tools, and the
portal API."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re

import httpx
import pytest
from sqlalchemy import select

from app.agent.memory import COLLECTION, MemoryScopes, MemoryStore, NoteError
from app.agent.runner import parse_notes
from app.bot.config_store import (
    EmbeddingSettings,
    ensure_embedding_config,
    ensure_memory_config,
)
from app.knowledge.embeddings import Embedder, EmbeddingError, Reranker, VectorCache, model_tag
from app.knowledge.search import fts_query, rrf
from app.knowledge.service import KnowledgeService
from app.knowledge.vectorstore import MemoryVectorStore, Point, QdrantStore, cosine
from app.models import AgentMemory, AgentUserProfile, BotConfig, EmbeddingCache
from app.security import encrypt_secret
from tests.test_agent import model, settle  # noqa: F401
from tests.test_llm import tool_use
from tests.test_responder import context

# --------------------------------------------------------------------------- #
# A fake embedding server whose vectors put synonyms together
# --------------------------------------------------------------------------- #

# Words of one group share a dimension, so they come out similar.
CONCEPTS = [
    {"cat", "cats", "kitten", "mèo"},
    {"coffee", "espresso", "latte", "cà", "phê"},
    {"deploy", "release", "ship", "rollout"},
    {"birthday", "sinh", "nhật"},
]
DIMENSIONS = 12


def concept_vector(text: str) -> list[float]:
    vector = [0.0] * DIMENSIONS
    for word in re.findall(r"\w+", text.casefold()):
        for index, group in enumerate(CONCEPTS):
            if word in group:
                vector[index] += 1.0
                break
        else:
            # Other words land on one of the remaining dimensions, a little.
            slot = len(CONCEPTS) + int(hashlib.sha1(word.encode()).hexdigest(), 16) % (
                DIMENSIONS - len(CONCEPTS)
            )
            vector[slot] += 0.3
    vector[-1] += 0.01  # never all zeros
    return vector


class FakeEmbeddings:
    """`/v1/embeddings` (OpenAI), `/embed` (TEI) and `/rerank`; records the calls."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "server says no"}})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": concept_vector(text)}
                        for i, text in reversed(list(enumerate(body["input"])))
                    ]
                },
            )
        if request.url.path.endswith("/embed"):
            return httpx.Response(200, json=[concept_vector(text) for text in body["inputs"]])
        if request.url.path.endswith("/rerank"):
            texts = body.get("texts") or body.get("documents")
            query = concept_vector(body["query"])
            scored = [(i, cosine(query, concept_vector(text))) for i, text in enumerate(texts)]
            if "texts" in body:
                return httpx.Response(200, json=[{"index": i, "score": s} for i, s in scored])
            return httpx.Response(
                200, json={"results": [{"index": i, "relevance_score": s} for i, s in scored]}
            )
        return httpx.Response(404)

    @property
    def embedded(self) -> list[str]:
        texts: list[str] = []
        for path, body in self.requests:
            if path.endswith("/embeddings"):
                texts.extend(body["input"])
            elif path.endswith("/embed"):
                texts.extend(body["inputs"])
        return texts


OPENAI = EmbeddingSettings(
    provider="openai", base_url="http://embed.invalid/v1", api_key="sk-e", model="embed-test"
)


@pytest.fixture
def server():
    return FakeEmbeddings()


@pytest.fixture
def http(server):
    return httpx.AsyncClient(transport=httpx.MockTransport(server))


@pytest.fixture
async def vectors(app_env, http):
    """The assistant searching an in-memory vector store with the fake server."""
    store = MemoryVectorStore()
    knowledge = KnowledgeService(app_env.session_factory, http_client=http, store=store)
    agent = app_env.bot.responder.agent
    agent.knowledge = knowledge
    agent.memory = MemoryStore(app_env.session_factory, knowledge)
    await set_embedding(app_env, provider="openai", base_url="http://embed.invalid/v1",
                        api_key="sk-e", model="embed-test")
    return store


async def set_embedding(runtime, **fields):
    async with runtime.session_factory() as session:
        config = await ensure_embedding_config(session)
        for name, value in fields.items():
            if name == "api_key":
                config.api_key_enc = encrypt_secret(value)
            else:
                setattr(config, name, value)
        (await session.get(BotConfig, 1)).config_version += 1
        await session.commit()
    runtime.config_store.invalidate()


async def set_memory(runtime, **fields):
    async with runtime.session_factory() as session:
        config = await ensure_memory_config(session)
        for name, value in fields.items():
            setattr(config, name, value)
        (await session.get(BotConfig, 1)).config_version += 1
        await session.commit()
    runtime.config_store.invalidate()


async def notes(runtime) -> list[AgentMemory]:
    async with runtime.session_factory() as db:
        return list((await db.scalars(select(AgentMemory).order_by(AgentMemory.id))).all())


def memory_of(runtime) -> MemoryStore:
    return runtime.bot.responder.agent.memory


# --------------------------------------------------------------------------- #
# Search building blocks
# --------------------------------------------------------------------------- #


def test_rrf_rewards_items_found_by_both_searches():
    merged = [item for item, _ in rrf([[1, 2, 3], [3, 4]])]
    assert merged[0] == 3
    assert set(merged) == {1, 2, 3, 4}


def test_fts_query_quotes_every_word():
    assert fts_query('Deploy "prod" AND NEAR(xy) a') == '"deploy" OR "prod" OR "and" OR "near" OR "xy"'
    assert fts_query("?? !") is None
    assert fts_query("phiên bản 2") == '"phiên" OR "bản" OR "2"'


def test_settings_name_their_vectors():
    assert OPENAI.configured and OPENAI.model_id
    assert EmbeddingSettings(provider="tei", base_url="http://tei:80").configured
    assert not EmbeddingSettings(provider="openai", base_url="http://x").configured
    other = dataclasses.replace(OPENAI, model="embed-other")
    assert model_tag(other.model_id) != model_tag(OPENAI.model_id)


# --------------------------------------------------------------------------- #
# Embeddings, their cache and the reranker
# --------------------------------------------------------------------------- #


async def test_openai_vectors_come_back_in_input_order(server, http):
    embedder = Embedder(OPENAI, http)
    first, second = await embedder.embed_documents(["a cat", "an espresso"])
    assert first == concept_vector("a cat") and second == concept_vector("an espresso")
    assert embedder.dimensions == DIMENSIONS
    path, body = server.requests[0]
    assert path == "/v1/embeddings" and body["model"] == "embed-test"


async def test_tei_and_prefixes(server, http):
    settings = EmbeddingSettings(
        provider="tei", base_url="http://tei.invalid", query_prefix="query: ", document_prefix="passage: "
    )
    embedder = Embedder(settings, http)
    await embedder.embed_query("cats")
    await embedder.embed_documents(["kitten"])
    assert server.embedded == ["query: cats", "passage: kitten"]


async def test_a_text_is_embedded_once(app_env, server, http):
    cache = VectorCache(app_env.session_factory)
    embedder = Embedder(OPENAI, http, cache)
    await embedder.embed_documents(["cat", "cat", "coffee"])
    await embedder.embed_documents(["coffee"])
    assert server.embedded == ["cat", "coffee"]

    # A new process finds them in the database.
    fresh = Embedder(OPENAI, http, VectorCache(app_env.session_factory))
    # Stored as float32, so a little rounding.
    [vector] = await fresh.embed_documents(["cat"])
    assert vector == pytest.approx(concept_vector("cat"), abs=1e-6)
    assert server.embedded == ["cat", "coffee"]
    async with app_env.session_factory() as db:
        assert len((await db.scalars(select(EmbeddingCache))).all()) == 2


async def test_embedding_errors_are_explained(server, http):
    server.status = 401
    with pytest.raises(EmbeddingError, match="HTTP 401: server says no"):
        await Embedder(OPENAI, http).embed_query("cat")


@pytest.mark.parametrize("provider", ["tei", "cohere"])
async def test_reranker_scores_follow_the_question(server, http, provider):
    settings = EmbeddingSettings(
        rerank_provider=provider, rerank_base_url="http://rerank.invalid", rerank_model="rr"
    )
    scores = await Reranker(settings, http).rerank("my cat", ["about coffee", "about a kitten"])
    assert scores[1] > scores[0]


# --------------------------------------------------------------------------- #
# Vector stores
# --------------------------------------------------------------------------- #


async def test_memory_vector_store_filters_and_ranks():
    store = MemoryVectorStore()
    assert await store.ensure_collection("memory", 3)
    assert not await store.ensure_collection("memory", 3)
    await store.upsert(
        "memory",
        [
            Point(1, [1, 0, 0], {"scope": "a", "model": "m"}),
            Point(2, [0.9, 0.1, 0], {"scope": "b", "model": "m"}),
            Point(3, [0, 1, 0], {"scope": "a", "model": "m"}),
        ],
    )
    hits = await store.search("memory", [1, 0, 0], limit=5, where={"scope": ["a"]})
    assert [hit.id for hit in hits] == [1, 3]
    hits = await store.search("memory", [1, 0, 0], limit=5, min_score=0.5)
    assert [hit.id for hit in hits] == [1, 2]
    # Another size of vector starts the collection afresh.
    assert await store.ensure_collection("memory", 4)
    assert await store.count("memory") == 0


async def test_qdrant_requests():
    seen: list[tuple[str, str, dict | None]] = []
    sizes: dict[str, int] = {}

    def qdrant(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        assert request.headers["api-key"] == "secret"
        path = request.url.path
        if request.method == "GET" and path == "/collections/bot_memory":
            if "bot_memory" not in sizes:
                return httpx.Response(404, json={"status": {"error": "Not found"}})
            return httpx.Response(
                200, json={"result": {"config": {"params": {"vectors": {"size": sizes["bot_memory"]}}}}}
            )
        if request.method == "PUT" and path == "/collections/bot_memory":
            sizes["bot_memory"] = body["vectors"]["size"]
        if path.endswith("/points/query"):
            return httpx.Response(
                200, json={"result": {"points": [{"id": 7, "score": 0.9, "payload": {"scope": "a"}}]}}
            )
        return httpx.Response(200, json={"result": {"status": "ok"}})

    store = QdrantStore(
        "http://qdrant.invalid:6333",
        http=httpx.AsyncClient(transport=httpx.MockTransport(qdrant)),
        api_key="secret",
        prefix="bot_",
    )
    assert await store.ensure_collection("memory", 3)
    assert not await store.ensure_collection("memory", 3)
    create = next(body for method, path, body in seen if method == "PUT" and path == "/collections/bot_memory")
    assert create["vectors"] == {"size": 3, "distance": "Cosine", "on_disk": True}
    assert create["quantization_config"]["scalar"]["type"] == "int8"
    indexed = [body["field_name"] for _, path, body in seen if path.endswith("/index")]
    assert indexed == ["scope", "model"]

    await store.upsert("memory", [Point(7, [1.0, 0.0, 0.0], {"scope": "a"})])
    hits = await store.search("memory", [1, 0, 0], limit=3, where={"scope": ["a", "global"]}, min_score=0.2)
    assert [(hit.id, hit.score) for hit in hits] == [(7, 0.9)]
    query = seen[-1][2]
    assert query["filter"] == {"must": [{"key": "scope", "match": {"any": ["a", "global"]}}]}
    assert query["score_threshold"] == 0.2


# --------------------------------------------------------------------------- #
# Memory store
# --------------------------------------------------------------------------- #


async def test_words_alone_find_notes_without_an_embedding_model(app_env):
    memory = memory_of(app_env)
    await memory.add("user:U1", "Bo drinks his coffee black", embedding=EmbeddingSettings())
    await memory.add("user:U1", "Bo has a cat named Mì", embedding=EmbeddingSettings())
    found = await memory.search("what about coffee?", ["user:U1"], embedding=EmbeddingSettings())
    assert [note.text for note in found] == ["Bo drinks his coffee black"]
    # Accents do not matter.
    assert await memory.search("ca phe", ["user:U1"], embedding=EmbeddingSettings()) == []
    await memory.add("user:U1", "Bo thích cà phê sữa đá", embedding=EmbeddingSettings())
    found = await memory.search("ca phe", ["user:U1"], embedding=EmbeddingSettings())
    assert [note.text for note in found] == ["Bo thích cà phê sữa đá"]


async def test_vectors_find_notes_that_share_no_word(app_env, vectors):
    memory = memory_of(app_env)
    snapshot = await app_env.config_store.current()
    await memory.add("user:U1", "Has a kitten called Mì", embedding=snapshot.embedding)
    await memory.add("user:U1", "Prefers espresso", embedding=snapshot.embedding)
    found = await memory.search("my cats", ["user:U1"], embedding=snapshot.embedding, limit=1)
    assert [note.text for note in found] == ["Has a kitten called Mì"]
    assert await vectors.count(COLLECTION) == 2


async def test_a_note_is_not_saved_twice(app_env, vectors):
    memory = memory_of(app_env)
    embedding = (await app_env.config_store.current()).embedding
    first, created = await memory.add("user:U1", "Likes  coffee", embedding=embedding)
    assert created and first.text == "Likes coffee"
    again, created = await memory.add("user:U1", "likes coffee", embedding=embedding)
    assert not created and again.id == first.id
    # Same meaning, other words: also a duplicate.
    again, created = await memory.add("user:U1", "Likes espresso", embedding=embedding)
    assert not created and again.id == first.id
    # Another scope may say the same.
    _, created = await memory.add("user:U2", "Likes coffee", embedding=embedding)
    assert created
    # Letters beyond ASCII are compared without case too.
    first, _ = await memory.add("user:U3", "Đi làm lúc 8 giờ", embedding=EmbeddingSettings())
    again, created = await memory.add("user:U3", "đi LÀM lúc 8 GIỜ", embedding=EmbeddingSettings())
    assert not created and again.id == first.id


async def test_notes_are_limited(app_env):
    memory = memory_of(app_env)
    with pytest.raises(NoteError, match="at most"):
        await memory.add("user:U1", "x" * 1001, embedding=EmbeddingSettings())
    with pytest.raises(NoteError, match="empty"):
        await memory.add("user:U1", "   ", embedding=EmbeddingSettings())


async def test_a_new_model_embeds_every_note_again(app_env, vectors, server):
    memory = memory_of(app_env)
    embedding = (await app_env.config_store.current()).embedding
    await memory.add("user:U1", "Has a kitten", embedding=embedding)
    await set_embedding(app_env, model="embed-v2")
    newer = (await app_env.config_store.current()).embedding
    assert await memory.pending_count(newer) == 1
    # Until it is redone, the old vector is not compared with the new model's.
    assert await memory._dense("cats", ["user:U1"], newer, None) == []
    assert await memory.index_pending(newer) == 1
    assert await memory.pending_count(newer) == 0
    assert await memory._dense("cats", ["user:U1"], newer, None) != []


async def test_notes_wait_for_the_embedding_server(app_env, vectors, server):
    memory = memory_of(app_env)
    embedding = (await app_env.config_store.current()).embedding
    server.status = 503
    note, created = await memory.add("user:U1", "Has a kitten", embedding=embedding)
    assert created
    assert await memory.pending_count(embedding) == 1
    # Word search still works meanwhile.
    assert [n.id for n in await memory.search("kitten", ["user:U1"], embedding=embedding)] == [note.id]
    server.status = 200
    # The server is left alone for a while after failing...
    embedder = memory.knowledge.embedder_for(embedding)
    assert embedder.breaker.open
    assert await memory.index_pending(embedding) == 0
    calls = len(server.requests)
    await memory.search("kitten", ["user:U1"], embedding=embedding)
    assert len(server.requests) == calls
    # ...and tried again after the pause.
    embedder.breaker.succeeded()
    assert await memory.index_pending(embedding) == 1


async def test_a_hanging_embedding_server_does_not_hold_up_answers(app_env, vectors, monkeypatch):
    from app.knowledge import embeddings

    monkeypatch.setattr(embeddings, "QUERY_TIMEOUT_SECONDS", 0.05)
    memory = memory_of(app_env)
    embedding = (await app_env.config_store.current()).embedding
    await memory.add("user:U1", "Has a kitten", embedding=EmbeddingSettings())

    calls = []

    def hang(request: httpx.Request) -> httpx.Response:
        calls.append(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("no answer", request=request)

    memory.knowledge._http = httpx.AsyncClient(transport=httpx.MockTransport(hang))
    memory.knowledge._built_from = None  # rebuild the embedder on the new client
    found = await memory.search("kitten", ["user:U1"], embedding=embedding)
    assert [note.text for note in found] == ["Has a kitten"]
    assert calls == [0.05]
    # The next question does not even try.
    await memory.search("kitten", ["user:U1"], embedding=embedding)
    assert len(calls) == 1


async def test_qdrant_is_left_alone_after_failing():
    calls = []

    def down(request):
        calls.append(request)
        raise httpx.ConnectError("refused", request=request)

    store = QdrantStore("http://qdrant.invalid", http=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    from app.knowledge.vectorstore import VectorStoreError

    for _ in range(3):
        with pytest.raises(VectorStoreError):
            await store.search("memory", [1.0], limit=1)
    assert len(calls) == 1


async def test_deleting_a_note_removes_its_vector(app_env, vectors):
    memory = memory_of(app_env)
    embedding = (await app_env.config_store.current()).embedding
    note, _ = await memory.add("user:U1", "Has a kitten", embedding=embedding)
    assert await memory.delete(note.id, ["user:U2"]) is None
    assert await memory.delete(note.id, ["user:U1"]) is not None
    assert await vectors.count(COLLECTION) == 0
    assert await memory.search("kitten", ["user:U1"], embedding=embedding) == []


async def test_reranking_orders_the_results(app_env, vectors, server):
    await set_embedding(app_env, rerank_provider="tei", rerank_base_url="http://rerank.invalid")
    memory = memory_of(app_env)
    embedding = (await app_env.config_store.current()).embedding
    for text in ("Drinks a latte every day", "Ships the release on Fridays", "Has two cats"):
        await memory.add("room:C1", text, embedding=embedding)
    found = await memory.search("when do we deploy", ["room:C1"], embedding=embedding, limit=2)
    assert found[0].text == "Ships the release on Fridays"
    assert any(path == "/rerank" for path, _ in server.requests)


def test_scopes_come_from_the_conversation():
    dm = MemoryScopes.for_conversation(context("hi", sender_id="U9"), "dm")
    assert dm.write == "user:U9" and dm.read == ("user:U9", "global")
    room = MemoryScopes.for_conversation(context("hi", room_type="c", room_id="C1"), "room")
    assert room.write == "room:C1" and "user:U1" not in room.read
    thread = MemoryScopes.for_conversation(context("hi", room_type="c", room_id="C1"), "thread")
    assert thread.write == "room:C1"


def test_parse_notes():
    assert parse_notes('Sure: {"notes": ["Bo likes tea", " ", 3]}') == ["Bo likes tea"]
    assert parse_notes("nothing") == []
    assert parse_notes('{"notes": "x"}') == []


# --------------------------------------------------------------------------- #
# The assistant remembering
# --------------------------------------------------------------------------- #


def tool_names(request: dict) -> list[str]:
    return [tool["function"]["name"] for tool in request.get("tools", [])]


async def test_the_model_saves_a_note_and_sees_it_later(app_env, gateway, model):
    model.answers = [tool_use("memory_save", note="Bo's cat is called Mì"), "Noted!"]
    reply = await app_env.bot.responder.respond(context("remember my cat is called Mì"))
    assert "Noted!" in reply.text
    assert {"memory_save", "memory_search", "memory_forget", "profile_update"} <= set(
        tool_names(model.requests[0])
    )
    [note] = await notes(app_env)
    assert (note.scope, note.text, note.created_by) == ("user:U1", "Bo's cat is called Mì", "bo")

    # A new conversation still knows.
    await app_env.bot.responder.respond(context("new"))
    model.answers = ["Mì"]
    await app_env.bot.responder.respond(context("what is my cat called?"))
    question = model.requests[-1]["messages"][-1]["content"]
    assert f"[#{note.id}] Bo's cat is called Mì" in question
    assert question.endswith("what is my cat called?")
    # Only the question is kept in the transcript.
    session = await app_env.bot.responder.agent.sessions.open("dm:R1", "dm", context("x"))
    assert session.messages[-2]["content"] == "what is my cat called?"


async def test_a_channel_never_sees_private_notes(app_env, gateway, model):
    await set_memory(app_env, prompt_notes=5)
    memory = memory_of(app_env)
    await memory.add("user:U1", "Bo's salary is secret", embedding=EmbeddingSettings())
    await memory.add("global", "The office opens at 8", embedding=EmbeddingSettings())
    from tests.test_llm import set_config

    await set_config(app_env, respond_in_channels=True)
    model.answers = [tool_use("memory_search", query="salary"), "I do not know."]
    await app_env.bot.responder.respond(context("@taskbot what is Bo's salary?", room_type="c", room_id="C1"))
    first = model.requests[0]
    assert "The office opens at 8" in first["messages"][-1]["content"]
    assert "salary is secret" not in json.dumps(first, ensure_ascii=False)
    assert "profile_update" not in tool_names(first)
    tool_reply = model.requests[1]["messages"][-1]
    assert tool_reply["role"] == "tool" and json.loads(tool_reply["content"]) == {"notes": []}


async def test_global_notes_cannot_be_forgotten_from_chat(app_env, gateway, model):
    memory = memory_of(app_env)
    shared, _ = await memory.add("global", "The office opens at 8", embedding=EmbeddingSettings())
    model.answers = [tool_use("memory_forget", id=shared.id), "Could not."]
    await app_env.bot.responder.respond(context("forget the office hours"))
    assert "no note" in model.requests[1]["messages"][-1]["content"]
    assert [n.id for n in await notes(app_env)] == [shared.id]


async def test_many_notes_are_searched_few_are_shown(app_env, gateway, model):
    await set_memory(app_env, prompt_notes=2)
    memory = memory_of(app_env)
    for text in ("Likes coffee", "Has a cat", "Ships releases on Friday", "Birthday in May"):
        await memory.add("user:U1", text, embedding=EmbeddingSettings())
    await app_env.bot.responder.respond(context("when is my birthday?"))
    question = model.requests[0]["messages"][-1]["content"]
    assert "Birthday in May" in question
    assert question.count("[#") <= 2


async def test_memory_can_be_turned_off(app_env, gateway, model):
    await set_memory(app_env, enabled=False)
    await memory_of(app_env).add("user:U1", "Likes coffee", embedding=EmbeddingSettings())
    await app_env.bot.responder.respond(context("coffee?"))
    assert "Likes coffee" not in json.dumps(model.requests[0])
    offered = [t["function"]["name"] for t in model.requests[0].get("tools", [])]
    assert not any(name.startswith(("memory_", "profile_")) for name in offered)


async def test_the_user_updates_their_profile(app_env, gateway, model):
    model.answers = [
        tool_use("profile_update", display_name="anh Bo", timezone="Asia/Ho_Chi_Minh", language="Vietnamese"),
        "Dạ.",
    ]
    await app_env.bot.responder.respond(context("gọi tôi là anh Bo nhé"))
    async with app_env.session_factory() as db:
        profile = await db.scalar(select(AgentUserProfile))
    assert (profile.rc_user_id, profile.username, profile.display_name) == ("U1", "bo", "anh Bo")

    await app_env.bot.responder.respond(context("hello"))
    system = model.requests[-1]["messages"][0]["content"]
    assert "Call them: anh Bo" in system and "Answer in: Vietnamese" in system


async def test_a_bad_timezone_is_refused(app_env, gateway, model):
    model.answers = [tool_use("profile_update", timezone="Mars/Olympus"), "Sorry."]
    await app_env.bot.responder.respond(context("my timezone is Mars"))
    assert "not a timezone" in model.requests[1]["messages"][-1]["content"]


async def test_facts_are_extracted_after_the_answer(app_env, gateway, model):
    await set_memory(app_env, auto_extract=True)
    from tests.test_agent import set_agent

    await set_agent(app_env, fast_model="glm-fast")
    model.answers = ["Nice to meet you, An.", '{"notes": ["The user is called An and works on billing"]}']
    await app_env.bot.responder.respond(context("I am An, I work on billing"))
    await settle(app_env)
    for task in list(app_env.bot.responder.agent._background):
        await task
    [note] = await notes(app_env)
    assert note.source == "auto" and note.scope == "user:U1"
    assert model.requests[-1]["model"] == "glm-fast"


async def test_the_turn_indexes_waiting_notes(app_env, gateway, model, vectors, server):
    memory = memory_of(app_env)
    await memory.add("user:U1", "Has a kitten", embedding=EmbeddingSettings())
    assert await vectors.count(COLLECTION) is None
    await app_env.bot.responder.respond(context("hi"))
    await memory._index_task
    assert await vectors.count(COLLECTION) == 1


# --------------------------------------------------------------------------- #
# Portal API
# --------------------------------------------------------------------------- #


async def test_embedding_settings_hide_the_key(admin_client):
    response = await admin_client.put(
        "/api/memory/embedding",
        json={"provider": "openai", "base_url": "http://embed.invalid/v1/", "api_key": "sk-x", "model": "e"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_api_key"] and body["configured"] and "api_key" not in body
    assert body["base_url"] == "http://embed.invalid/v1"
    assert body["vector_db_configured"] is False


async def test_embedding_test_reports_dimensions(admin_client, app_env, http):
    app_env.bot.responder.agent.knowledge._http = http
    response = await admin_client.post(
        "/api/memory/embedding/test",
        json={
            "provider": "openai", "base_url": "http://embed.invalid/v1", "api_key": "k", "model": "e",
            "rerank_provider": "cohere", "rerank_base_url": "http://rr.invalid", "rerank_model": "r",
        },
    )
    body = response.json()
    assert body["ok"] and body["dimensions"] == DIMENSIONS
    assert body["rerank_ok"] is not None


async def test_notes_api(admin_client, app_env):
    created = await admin_client.post("/api/memory/notes", json={"scope": "global", "text": "Office opens at 8"})
    assert created.status_code == 201, created.text
    note = created.json()
    duplicate = await admin_client.post("/api/memory/notes", json={"scope": "global", "text": "office opens at 8"})
    assert duplicate.status_code == 409
    bad = await admin_client.post("/api/memory/notes", json={"scope": "anything", "text": "x"})
    assert bad.status_code == 422

    listed = (await admin_client.get("/api/memory/notes", params={"q": "office"})).json()
    assert listed["total"] == 1 and listed["items"][0]["source"] == "portal"
    found = (await admin_client.get("/api/memory/search", params={"q": "office", "scope": "global"})).json()
    assert [n["id"] for n in found] == [note["id"]]
    scopes = (await admin_client.get("/api/memory/scopes")).json()
    assert scopes == [{"scope": "global", "count": 1, "label": "everyone"}]

    updated = await admin_client.put(f"/api/memory/notes/{note['id']}", json={"text": "Office opens at 9"})
    assert updated.json()["text"] == "Office opens at 9"
    assert (await admin_client.delete(f"/api/memory/notes/{note['id']}")).status_code == 204
    assert (await admin_client.delete(f"/api/memory/notes/{note['id']}")).status_code == 404

    status = (await admin_client.get("/api/memory/status")).json()
    assert status["notes"] == 0 and status["semantic"] is False


async def test_profiles_api(admin_client, app_env):
    response = await admin_client.put(
        "/api/memory/profiles/U7", json={"username": "@an", "display_name": "An", "timezone": "Asia/Ho_Chi_Minh"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["username"] == "an"
    assert (await admin_client.put("/api/memory/profiles/U7", json={"timezone": "Nowhere"})).status_code == 422
    [profile] = (await admin_client.get("/api/memory/profiles")).json()
    assert profile["display_name"] == "An"
    assert (await admin_client.delete("/api/memory/profiles/U7")).status_code == 204
    assert await app_env.bot.responder.agent.profiles.get("U7") is None


async def test_viewers_cannot_read_notes(client, app_env):
    from app.models import PortalUser, Role
    from app.security import hash_password

    async with app_env.session_factory() as session:
        session.add(PortalUser(email="v@example.com", password_hash=hash_password("pw-123456"), role=Role.viewer.value))
        await session.commit()
    login = await client.post("/api/auth/login", json={"email": "v@example.com", "password": "pw-123456"})
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    assert (await client.get("/api/memory/notes")).status_code == 403
    assert (await client.get("/api/memory/config")).status_code == 200
