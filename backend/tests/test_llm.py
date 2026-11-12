"""The "llm" responder: unmatched messages answered by an OpenAI-compatible model.

The model is a fake endpoint behind httpx's MockTransport, so the real OpenAI
SDK builds the requests and parses the replies.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.bot.config_store import LLMSettings
from app.bot.llm import LLMChat, LLMService, ToolCall, Toolbox
from app.config import get_settings
from app.bot.streaming import STREAM_CURSOR
from app.integrations import executor
from app.models import BotConfig, Integration, IntegrationParam, LLMConfig
from app.security import decrypt_secret, encrypt_secret
from tests.test_responder import add, context, incoming


def tool_use(name: str, **arguments) -> dict:
    """A canned answer in which the model calls a tool."""
    return {"tool": name, "arguments": json.dumps(arguments)}


class FakeModel:
    """Answers chat completions with canned replies and records the requests.

    An answer is either text or a `tool_use(...)`.
    """

    def __init__(self, *answers: str, status: int = 200) -> None:
        self.answers = list(answers)
        self.status = status
        self.requests: list[dict] = []
        self.headers: list[httpx.Headers] = []
        self.hang = False
        self.side_requests: list[dict] = []
        # What indexing and judging requests get; by default nothing usable,
        # so documents keep their heading cut and search keeps its order.
        self.on_index = lambda body: '{"sections": []}'
        self.on_judge = lambda body: "unsure"
        # Asked to keep an answer for next time; by default nothing is kept.
        self.on_learn = lambda body: '{"reusable": false}'
        # Asked to split a long request into steps; by default no plan.
        self.on_plan = lambda body: "no plan"
        # Seconds between streamed pieces, as a real model takes; 0 sends all at once.
        self.token_delay = 0.0

    # Background work for the knowledge base, answered apart from the chat.
    INDEX_PROMPT = "You index a document"
    JUDGE_PROMPT = "You check search results"
    LEARN_PROMPT = "You prepare an answer to keep"
    PLAN_PROMPT = "You plan work an assistant will do"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = next((m.get("content") or "" for m in body.get("messages", []) if m.get("role") == "system"), "")
        sides = (self.INDEX_PROMPT, self.JUDGE_PROMPT, self.LEARN_PROMPT, self.PLAN_PROMPT)
        if system.startswith(sides):
            self.side_requests.append(body)
            if system.startswith(self.INDEX_PROMPT):
                handler = self.on_index
            elif system.startswith(self.JUDGE_PROMPT):
                handler = self.on_judge
            elif system.startswith(self.PLAN_PROMPT):
                handler = self.on_plan
            else:
                handler = self.on_learn
            return _completion(body, handler(body))
        self.requests.append(body)
        self.headers.append(request.headers)
        if self.hang:
            raise httpx.ReadTimeout("no answer", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "nope"}})
        answer = self.answers.pop(0) if self.answers else "ok"
        if isinstance(answer, dict):
            call = {
                "id": f"call-{len(self.requests)}",
                "type": "function",
                "function": {"name": answer["tool"], "arguments": answer["arguments"]},
            }
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            # Streamed the way OpenAI does it: id and name, then the arguments in two halves.
            half = len(answer["arguments"]) // 2
            deltas = [
                {"tool_calls": [{"index": 0, **call, "function": {"name": answer["tool"], "arguments": ""}}]},
                {"tool_calls": [{"index": 0, "function": {"arguments": answer["arguments"][:half]}}]},
                {"tool_calls": [{"index": 0, "function": {"arguments": answer["arguments"][half:]}}]},
            ]
        else:
            message = {"role": "assistant", "content": answer}
            deltas = [{"content": word + " "} for word in answer.split(" ")]

        if not body.get("stream"):
            return httpx.Response(
                200,
                json={
                    "id": "c1",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body["model"],
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                },
            )
        events = [
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            for delta in deltas
        ]
        if self.token_delay:
            delay = self.token_delay

            async def paced():
                for event in events:
                    yield f"data: {json.dumps(event)}\n\n".encode()
                    await asyncio.sleep(delay)
                yield b"data: [DONE]\n\n"

            return httpx.Response(200, content=paced(), headers={"content-type": "text/event-stream"})
        payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(
            200,
            content=(payload + "data: [DONE]\n\n").encode(),
            headers={"content-type": "text/event-stream"},
        )

    @property
    def last_messages(self) -> list[dict]:
        return self.requests[-1]["messages"]


async def set_config(runtime, **fields):
    async with runtime.session_factory() as session:
        config = await session.get(BotConfig, 1)
        for name, value in fields.items():
            setattr(config, name, value)
        config.config_version += 1
        await session.commit()
    runtime.config_store.invalidate()


async def set_llm(runtime, **fields):
    """Change the stored LLM settings, as the portal would."""
    async with runtime.session_factory() as session:
        config = await session.get(LLMConfig, 1)
        for name, value in fields.items():
            if name == "api_key":
                config.api_key_enc = encrypt_secret(value)
            else:
                setattr(config, name, value)
        (await session.get(BotConfig, 1)).config_version += 1
        await session.commit()
    runtime.config_store.invalidate()


def _completion(body: dict, content: str) -> httpx.Response:
    if body.get("stream"):
        event = {
            "id": "side",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": body["model"],
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }
        return httpx.Response(
            200,
            content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(
        200,
        json={
            "id": "side",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
        },
    )


def fake_transport(fake: FakeModel) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake))


@pytest.fixture
async def model(app_env):
    """LLM mode on, talking to a fake model."""
    fake = FakeModel()
    app_env.bot.responder.llm = LLMService(http_client=fake_transport(fake))
    await set_llm(
        app_env,
        base_url="http://llm.invalid/v1",
        api_key="sk-test",
        model="glm-test",
        history_messages=4,
    )
    await set_config(app_env, responder_mode="llm", bot_username="taskbot")
    return fake


async def test_an_unmatched_dm_is_answered_by_the_model(app_env, gateway, model):
    model.answers = ["Hello there!"]
    reply = await app_env.bot.responder.respond(context("hi, who are you?"))

    assert reply is not None and reply.text == "Hello there!" and reply.error is None
    assert model.requests[0]["model"] == "glm-test"
    system, question = model.last_messages
    assert system["role"] == "system" and "taskbot" in system["content"]
    assert question == {"role": "user", "content": "hi, who are you?"}


async def test_commands_still_win_over_the_model(app_env, gateway, model):
    reply = await app_env.bot.responder.respond(context("help"))
    assert reply is not None and "What I can do" in reply.text
    assert model.requests == []


async def test_rules_mode_ignores_the_model(app_env, gateway, model):
    await set_config(app_env, responder_mode="rules")
    reply = await app_env.bot.responder.respond(context("something random"))
    assert reply is not None and "did not understand" in reply.text
    assert model.requests == []


async def test_the_conversation_is_remembered(app_env, gateway, model):
    model.answers = ["Nice to meet you, An.", "You are An."]
    await app_env.bot.responder.respond(context("my name is An"))
    reply = await app_env.bot.responder.respond(context("what is my name?"))

    assert reply.text == "You are An."
    assert [m["content"] for m in model.last_messages[1:]] == [
        "my name is An",
        "Nice to meet you, An.",
        "what is my name?",
    ]


async def test_rooms_do_not_share_a_conversation(app_env, gateway, model):
    await app_env.bot.responder.respond(context("secret", room_id="R1"))
    await app_env.bot.responder.respond(context("hello", room_id="R2"))
    assert len(model.last_messages) == 2


async def test_in_a_channel_the_model_only_answers_mentions(app_env, gateway, model):
    quiet = await app_env.bot.responder.respond(context("/unknown thing", room_type="c"))
    assert quiet is None and model.requests == []

    model.answers = ["Sure."]
    reply = await app_env.bot.responder.respond(
        context("@taskbot can you help?", room_type="c")
    )
    assert reply.text == "Sure."
    assert model.last_messages[-1]["content"] == "@bo: can you help?"


async def test_a_model_error_is_explained_not_raised(app_env, gateway, model):
    model.status = 401
    reply = await app_env.bot.responder.respond(context("hi"))
    assert reply.text.startswith(":warning:") and "HTTP 401" in reply.error
    assert "sk-test" not in reply.text


async def test_an_empty_answer_is_reported(app_env, gateway, model):
    model.answers = ["   "]
    reply = await app_env.bot.responder.respond(context("hi"))
    assert reply.error and "empty" in reply.error


async def test_streaming_mode_shows_the_answer_as_it_arrives(app_env, gateway, model):
    await set_config(app_env, reply_mode="stream", stream_interval_ms=50, stream_chunk_chars=4)
    model.answers = ["one two three four five six"]
    model.token_delay = 0.03

    await app_env.bot._handle(incoming("tell me something"))

    assert model.requests[0]["stream"] is True
    assert len(gateway.sent) == 1
    assert gateway.last_text == "one two three four five six"
    assert gateway.updates and gateway.updates[0][1].endswith(STREAM_CURSOR)


async def test_the_conversation_outlives_a_restart(app_env, gateway, model):
    model.answers = ["Nice to meet you, An.", "You are An."]
    await app_env.bot.responder.respond(context("my name is An"))
    # A new runner has nothing in memory, so it reads the transcript back.
    app_env.bot.responder.agent.sessions._hot.clear()
    reply = await app_env.bot.responder.respond(context("what is my name?"))

    assert reply.text == "You are An."
    assert [m["content"] for m in model.last_messages[1:]] == [
        "my name is An",
        "Nice to meet you, An.",
        "what is my name?",
    ]


def test_the_model_needs_url_key_and_model():
    settings = LLMSettings(base_url="http://llm.invalid/v1", api_key="sk-test")
    assert LLMChat.from_settings(settings) is None
    chat = LLMChat.from_settings(LLMSettings(base_url="http://llm.invalid/v1", api_key="k", model="m"))
    assert chat.model == "m"


# --------------------------------------------------------------------------- #
# Settings from the portal
# --------------------------------------------------------------------------- #


async def test_the_gateway_key_and_model_come_from_the_stored_settings(app_env, gateway, model):
    await app_env.bot.responder.respond(context("hi"))
    assert model.requests[-1]["model"] == "glm-test"
    assert model.headers[-1]["authorization"] == "Bearer sk-test"

    await set_llm(app_env, model="other-model", api_key="sk-new")
    await app_env.bot.responder.respond(context("hi again"))
    assert model.requests[-1]["model"] == "other-model"
    assert model.headers[-1]["authorization"] == "Bearer sk-new"


async def test_a_custom_system_prompt_is_used(app_env, gateway, model):
    await set_llm(app_env, system_prompt="You are {bot}. Always answer in Vietnamese.")
    await app_env.bot.responder.respond(context("hi"))
    assert model.last_messages[0]["content"] == "You are taskbot. Always answer in Vietnamese."


async def test_max_tokens_and_temperature_are_sent_when_set(app_env, gateway, model):
    await app_env.bot.responder.respond(context("hi"))
    assert "max_tokens" not in model.requests[-1] and "temperature" not in model.requests[-1]

    await set_llm(app_env, max_tokens=300, temperature=0.2)
    await app_env.bot.responder.respond(context("hi"))
    assert model.requests[-1]["max_tokens"] == 300 and model.requests[-1]["temperature"] == 0.2


async def test_a_slow_gateway_times_out_with_a_clear_message(app_env, gateway, model):
    await set_llm(app_env, timeout_seconds=5)
    model.hang = True
    reply = await app_env.bot.responder.respond(context("hi"))
    assert reply.error == "the language model did not answer within 5s"
    # No retry: the admin's timeout is the whole wait.
    assert len(model.requests) == 1


async def test_without_a_complete_configuration_the_fallback_answers(app_env, gateway, model):
    await set_llm(app_env, api_key=None)
    reply = await app_env.bot.responder.respond(context("something random"))
    assert "did not understand" in reply.text and model.requests == []


async def test_history_length_follows_the_setting(app_env, gateway, model):
    for n in range(3):
        await app_env.bot.responder.respond(context(f"q{n}"))
    assert len(model.last_messages) == 1 + 4 + 1  # system, 4 remembered, question

    await set_llm(app_env, history_messages=0)
    await app_env.bot.responder.respond(context("again"))
    assert len(model.last_messages) == 2


# --------------------------------------------------------------------------- #
# Rocket.Chat formatting
# --------------------------------------------------------------------------- #


async def test_the_answer_is_formatted_for_rocketchat(app_env, gateway, model):
    model.answers = ["## Kết quả\n\nĐây là **đậm** và *nghiêng*.\n\n* một\n* hai"]
    reply = await app_env.bot.responder.respond(context("hi"))
    assert reply.text == "*Kết quả*\n\nĐây là *đậm* và _nghiêng_.\n\n- một\n- hai"


async def test_the_model_remembers_its_own_markdown(app_env, gateway, model):
    model.answers = ["<think>hmm</think>Là **An**."]
    await app_env.bot.responder.respond(context("tên tôi?"))
    await app_env.bot.responder.respond(context("chắc không?"))
    assert model.last_messages[-2] == {"role": "assistant", "content": "Là **An**."}


async def test_formatting_can_be_turned_off(app_env, gateway, model):
    await set_llm(app_env, format_for_rocketchat=False)
    model.answers = ["<think>x</think>Already *bold* in Rocket.Chat."]
    reply = await app_env.bot.responder.respond(context("hi"))
    assert reply.text == "Already *bold* in Rocket.Chat."
    assert "no headings or tables" in model.last_messages[0]["content"]


async def test_streamed_text_is_formatted_as_it_arrives(app_env, gateway, model):
    await set_config(app_env, reply_mode="stream", stream_interval_ms=50, stream_chunk_chars=4)
    model.answers = ["<think>plan the answer</think>Đây là **rất đậm** và *nghiêng* nhé bạn"]
    model.token_delay = 0.03

    await app_env.bot._handle(incoming("tell me"))

    assert gateway.last_text == "Đây là *rất đậm* và _nghiêng_ nhé bạn"
    shown = [gateway.sent[0].text, *(text for _, text in gateway.updates)]
    assert len(gateway.updates) > 2 and gateway.updates[0][1].endswith(STREAM_CURSOR)
    assert all("think" not in text and "plan the" not in text for text in shown)
    assert all("**" not in text for text in shown)


# --------------------------------------------------------------------------- #
# Portal API
# --------------------------------------------------------------------------- #


async def test_the_api_never_returns_the_key(admin_client, app_env):
    response = await admin_client.put(
        "/api/llm",
        json={
            "base_url": "https://gateway.example.com/v1/",
            "api_key": "sk-secret",
            "model": "glm-4.6",
            "timeout_seconds": 30,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["base_url"] == "https://gateway.example.com/v1"
    assert body["has_api_key"] is True and body["configured"] is True
    assert "sk-secret" not in response.text
    assert body["timeout_seconds"] == 30 and body["enabled"] is False

    async with app_env.session_factory() as session:
        stored = await session.get(LLMConfig, 1)
        assert stored.api_key_enc != "sk-secret" and decrypt_secret(stored.api_key_enc) == "sk-secret"

    # Omitting the key keeps it; "" clears it.
    body = (await admin_client.put("/api/llm", json={"model": "glm-5"})).json()
    assert body["has_api_key"] is True and body["model"] == "glm-5"
    body = (await admin_client.put("/api/llm", json={"api_key": ""})).json()
    assert body["has_api_key"] is False and body["configured"] is False


async def test_extra_fields_for_quick_jobs_must_be_a_json_object(admin_client):
    for bad in ('{"a": ', '["x"]', '{"model": "other"}'):
        response = await admin_client.put("/api/llm", json={"quick_extra_body": bad})
        assert response.status_code == 422, bad
    fields = '{"chat_template_kwargs": {"enable_thinking": false}}'
    body = (await admin_client.put("/api/llm", json={"quick_extra_body": fields})).json()
    assert body["quick_extra_body"] == fields
    body = (await admin_client.put("/api/llm", json={"quick_extra_body": ""})).json()
    assert body["quick_extra_body"] == ""


async def test_quick_jobs_carry_the_extra_fields_and_answers_do_not(app_env, gateway, model):
    await set_llm(app_env, quick_extra_body='{"reasoning_effort": "low"}')
    chat = app_env.bot.responder.llm.chat_for((await app_env.config_store.current()).llm)
    await chat.complete([{"role": "user", "content": "hi"}], quick=True)
    await chat.complete([{"role": "user", "content": "hi"}])
    assert model.requests[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in model.requests[1]


async def test_turning_the_model_on_needs_a_complete_configuration(admin_client):
    response = await admin_client.put("/api/llm", json={"enabled": True})
    assert response.status_code == 422
    response = await admin_client.put("/api/config", json={"responder_mode": "llm"})
    assert response.status_code == 422
    assert (await admin_client.get("/api/config")).json()["llm_available"] is False

    response = await admin_client.put(
        "/api/llm",
        json={"base_url": "http://llm.invalid/v1", "api_key": "k", "model": "m", "enabled": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    config = (await admin_client.get("/api/config")).json()
    assert config["responder_mode"] == "llm" and config["llm_model"] == "m"

    # Clearing the key while on is refused, so the bot is never left half set up.
    assert (await admin_client.put("/api/llm", json={"api_key": ""})).status_code == 422


async def test_a_bad_gateway_url_is_refused(admin_client):
    response = await admin_client.put("/api/llm", json={"base_url": "file:///etc/passwd"})
    assert response.status_code == 422


async def test_private_gateways_follow_the_integration_rule(admin_client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "allow_private_integration_hosts", False)
    response = await admin_client.put("/api/llm", json={"base_url": "http://127.0.0.1:4000/v1"})
    assert response.status_code == 422 and "private address" in response.json()["detail"]


async def test_viewers_cannot_change_the_model(client, app_env):
    from app.models import PortalUser, Role
    from app.security import hash_password

    async with app_env.session_factory() as session:
        session.add(PortalUser(email="v@example.com", password_hash=hash_password("pw-123456"), role=Role.viewer.value))
        await session.commit()
    login = await client.post("/api/auth/login", json={"email": "v@example.com", "password": "pw-123456"})
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

    assert (await client.get("/api/llm")).status_code == 200
    assert (await client.put("/api/llm", json={"model": "x"})).status_code == 403
    assert (await client.post("/api/llm/test", json={})).status_code == 403


async def test_the_connection_can_be_tested_before_saving(admin_client, monkeypatch):
    fake = FakeModel("Mình đang **hoạt động**.")
    build = LLMChat.from_settings
    monkeypatch.setattr(
        "app.api.llm.LLMChat.from_settings",
        lambda settings: build(settings, http_client=fake_transport(fake)),
    )

    response = await admin_client.post(
        "/api/llm/test",
        json={"base_url": "http://llm.invalid/v1", "api_key": "sk-try", "model": "glm-try"},
    )
    body = response.json()
    assert body["ok"] is True, body
    assert body["reply"] == "Mình đang *hoạt động*." and "glm-try" in body["message"]
    assert fake.headers[0]["authorization"] == "Bearer sk-try"
    # Nothing was saved.
    assert (await admin_client.get("/api/llm")).json()["has_api_key"] is False


async def test_a_failed_connection_test_says_why(admin_client, monkeypatch):
    fake = FakeModel(status=401)
    build = LLMChat.from_settings
    monkeypatch.setattr(
        "app.api.llm.LLMChat.from_settings",
        lambda settings: build(settings, http_client=fake_transport(fake)),
    )
    body = (await admin_client.post("/api/llm/test", json={"base_url": "http://llm.invalid/v1", "api_key": "k", "model": "m"})).json()
    assert body["ok"] is False and "HTTP 401" in body["message"]

    body = (await admin_client.post("/api/llm/test", json={})).json()
    assert body["ok"] is False and body["message"] == "Missing: gateway URL, API key, model"


# --------------------------------------------------------------------------- #
# Integrations as tools
# --------------------------------------------------------------------------- #


@pytest.fixture
async def weather_api(app_env, monkeypatch):
    """A `weather` integration exposed to the model, served by a fake API."""
    calls: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/Atlantis"):
            return httpx.Response(404, json={"detail": "unknown city"})
        return httpx.Response(200, json={"temp": 26, "sky": "sunny"})

    async def execute(spec, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
            return await executor.execute_integration(spec, client=client, **kwargs)

    monkeypatch.setattr("app.agent.tools.integrations.execute_integration", execute)

    integration = Integration(
        name="weather.now",
        description="Current weather for a city",
        base_url="https://api.example.com",
        path_template="/w/{{ params.city }}",
        query_template='{"who": "{{ user.username }}"}',
        llm_tool=True,
    )
    integration.params = [
        IntegrationParam(name="city", position=0, required=True, description="City name")
    ]
    await add(app_env, integration, Integration(name="private", base_url="https://x.example.com"))
    return calls


async def without_memory(runtime) -> None:
    """Turn memory and jobs off, so only integrations are offered as tools."""
    from app.bot.config_store import ensure_agent_config, ensure_memory_config

    async with runtime.session_factory() as session:
        (await ensure_memory_config(session)).enabled = False
        (await ensure_agent_config(session)).jobs_enabled = False
        (await session.get(BotConfig, 1)).config_version += 1
        await session.commit()
    runtime.config_store.invalidate()


async def test_only_integrations_marked_as_tools_are_offered(app_env, gateway, model, weather_api):
    await without_memory(app_env)
    await app_env.bot.responder.respond(context("hello"))

    tools = model.requests[0]["tools"]
    assert [tool["function"]["name"] for tool in tools] == ["weather_now"]
    assert tools[0]["function"]["parameters"]["required"] == ["city"]


async def test_no_tools_are_sent_when_none_are_marked(app_env, gateway, model):
    await without_memory(app_env)
    await app_env.bot.responder.respond(context("hello"))
    assert "tools" not in model.requests[0]


async def test_the_model_can_call_an_integration(app_env, gateway, model, weather_api):
    model.answers = [tool_use("weather_now", city="Hanoi"), "It is 26°C and sunny in Hanoi."]

    reply = await app_env.bot.responder.respond(context("how is the weather in Hanoi?"))

    assert reply.text == "It is 26°C and sunny in Hanoi." and reply.error is None
    (request,) = weather_api
    assert request.url.path == "/w/Hanoi" and request.url.params["who"] == "bo"
    call, result = model.last_messages[-2:]
    assert call["tool_calls"][0]["function"]["name"] == "weather_now"
    assert result["role"] == "tool" and result["tool_call_id"] == call["tool_calls"][0]["id"]
    assert json.loads(result["content"]) == {"status": 200, "response": {"temp": 26, "sky": "sunny"}}


async def test_tool_calls_work_while_streaming(app_env, gateway, model, weather_api):
    await set_config(app_env, reply_mode="stream", stream_interval_ms=50)
    model.answers = [tool_use("weather_now", city="Hanoi"), "Sunny, 26°C."]

    await app_env.bot._handle(incoming("weather in Hanoi?"))

    assert weather_api[0].url.path == "/w/Hanoi"
    assert gateway.last_text == "Sunny, 26°C."


async def test_a_failing_integration_is_reported_to_the_model(app_env, gateway, model, weather_api):
    model.answers = [tool_use("weather_now", city="Atlantis"), "I could not find Atlantis."]

    reply = await app_env.bot.responder.respond(context("weather in Atlantis?"))

    assert reply.text == "I could not find Atlantis."
    result = json.loads(model.last_messages[-1]["content"])
    assert result["error"] == "HTTP 404" and result["response"] == {"detail": "unknown city"}


async def test_a_missing_argument_is_reported_to_the_model(app_env, gateway, model, weather_api):
    model.answers = [tool_use("weather_now"), "Which city?"]

    reply = await app_env.bot.responder.respond(context("weather?"))

    assert reply.text == "Which city?" and weather_api == []
    assert "city" in json.loads(model.last_messages[-1]["content"])["error"]


async def test_after_its_tool_rounds_the_model_answers_without_tools(
    app_env, gateway, model, weather_api, monkeypatch
):
    monkeypatch.setattr(get_settings(), "max_tool_rounds", 3)  # APP_MAX_TOOL_ROUNDS
    model.answers = [tool_use("weather_now", city="Hanoi")] * 3 + ["Hanoi is sunny, as far as I got."]

    reply = await app_env.bot.responder.respond(context("weather?"))

    assert reply.text == "Hanoi is sunny, as far as I got." and not reply.error
    assert len(weather_api) == 3
    last = model.requests[-1]
    assert last["tool_choice"] == "none" and last["tools"]
    assert "Do not call any more tools" in last["messages"][-1]["content"]


async def test_a_model_that_never_stops_calling_tools_is_cut_off(app_env, gateway, model, weather_api, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_tool_rounds", 2)
    model.answers = [tool_use("weather_now", city="Hanoi")] * 3

    reply = await app_env.bot.responder.respond(context("weather?"))

    assert reply.error and "kept calling tools" in reply.error
    assert len(weather_api) == 2


async def test_toolbox_rejects_unknown_tools_and_bad_arguments():
    toolbox = Toolbox([], {})
    assert not toolbox
    assert "no tool" in json.loads(await toolbox.call(ToolCall("1", "nope", "{}")))["error"]

    toolbox = Toolbox([executor.IntegrationSpec(1, "a", "", "https://x.example.com", "GET")], {})
    assert "JSON object" in json.loads(await toolbox.call(ToolCall("1", "a", "[1]")))["error"]
    assert "JSON object" in json.loads(await toolbox.call(ToolCall("1", "a", "{oops")))["error"]


def test_tool_names_are_made_valid_and_unique():
    specs = [
        executor.IntegrationSpec(n, name, "", "https://x.example.com", "GET")
        for n, name in enumerate(["get.user", "get_user", "jira-search"])
    ]
    names = [tool["function"]["name"] for tool in Toolbox(specs, {}).schemas]
    assert names == ["get_user", "get_user_2", "jira-search"]
