"""Replies from a language model behind an OpenAI-compatible API.

Used when `bot_config.responder_mode` is "llm": commands still answer first,
and whatever matches none of them is sent to the model instead of getting the
fallback reply. The gateway, key, model and timeout are set in the portal
(`llm_config`); `LLMService` rebuilds its client whenever they change.

Conversations, their transcripts and the per-turn deadline live in
`app.agent`; this module is only the client: one chat-completions endpoint,
streaming, and the loop that runs the tools the model asks for (see
`app.agent.tools`) until it answers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from app.agent.tools import (
    MAX_TOOL_RESULT_CHARS,
    ToolCall,
    ToolRegistry,
    is_error_result,
    tool_result,
)
from app.agent.prompt import build_messages
from app.agent.timing import ToolTiming, TurnTimer
from app.agent.tools.integrations import Toolbox
from app.bot.config_store import LLMSettings

log = logging.getLogger(__name__)

_PROMPT_START = (
    "You are {bot}, a helpful assistant chatting with people in Rocket.Chat. "
    "Today is {today}. "
    "Answer concisely, in the language the user writes in. "
)
_PROMPT_END = "The bot also has commands; users can send `help` to list them."
# Used when the answer is converted for Rocket.Chat afterwards (app.bot.rcformat).
DEFAULT_SYSTEM_PROMPT = _PROMPT_START + "Format with standard Markdown. " + _PROMPT_END
# Used when it is posted as written, so the model must write Rocket.Chat's syntax.
DEFAULT_SYSTEM_PROMPT_RAW = (
    _PROMPT_START
    + "Format for Rocket.Chat: *bold*, _italic_, ~strikethrough~, `code`, "
    "```code blocks```, [text](url) links and - bullet lists; no headings or "
    "tables. "
    + _PROMPT_END
)
# Connecting gets this long at most, even when the timeout is longer.
CONNECT_TIMEOUT_SECONDS = 10.0
# Rounds of tool calls in one answer before the model is cut off.
MAX_TOOL_ROUNDS = 5
Message = dict[str, Any]
# A tool that has not answered by then fails, and the model is told so.
TOOL_TIMEOUT_SECONDS = 30.0
# Events the loop reports while it runs: ("tool_start", name) and
# ("tool_end", name, ok).
EventHandler = Callable[..., None]

__all__ = [
    "MAX_TOOL_RESULT_CHARS",
    "LLMChat",
    "LLMError",
    "LLMService",
    "ToolCall",
    "Toolbox",
]


class LLMError(Exception):
    """The model could not answer; the message is safe to show in chat."""


# Request fields the client sets itself; extra fields may not replace them.
_RESERVED_FIELDS = frozenset({"model", "messages", "stream", "tools", "max_tokens"})


def parse_extra_body(text: str) -> dict[str, Any]:
    """The JSON object in `text` ({} when empty); ValueError if it is not one."""
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError("must be a JSON object, like {\"key\": \"value\"}")
    clash = sorted(_RESERVED_FIELDS & value.keys())
    if clash:
        raise ValueError(f"cannot set {', '.join(clash)}")
    return value


class LLMChat:
    """One chat-completions endpoint and the model to ask there."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str = "",
        timeout_seconds: float = 60.0,
        max_tokens: int | None = None,
        temperature: float | None = None,
        quick_extra_body: dict[str, Any] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        # Sent with quick side jobs (`complete(quick=True)`).
        self.quick_extra_body = dict(quick_extra_body or {})
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            # Applies to each wait for the gateway: the whole answer, or when
            # streaming, the first token and each one after it.
            timeout=httpx.Timeout(
                timeout_seconds, connect=min(CONNECT_TIMEOUT_SECONDS, timeout_seconds)
            ),
            # A retry would double the wait the admin configured.
            max_retries=0,
            http_client=http_client,
        )

    @classmethod
    def from_settings(
        cls, settings: LLMSettings, *, http_client: httpx.AsyncClient | None = None
    ) -> LLMChat | None:
        """None unless the gateway, key and model are all set."""
        if not settings.configured:
            return None
        default_prompt = (
            DEFAULT_SYSTEM_PROMPT if settings.format_for_rocketchat else DEFAULT_SYSTEM_PROMPT_RAW
        )
        return cls(
            base_url=settings.base_url,
            api_key=settings.api_key or "",
            model=settings.model,
            system_prompt=settings.system_prompt.strip() or default_prompt,
            timeout_seconds=settings.timeout_seconds,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
            quick_extra_body=_extra_body_or_none(settings.quick_extra_body),
            http_client=http_client,
        )

    async def aclose(self) -> None:
        await self._client.close()

    def build_messages(
        self,
        question: str,
        history: list[Message],
        *,
        bot_name: str,
        timezone_name: str | None = None,
        summary: str = "",
    ) -> list[Message]:
        return build_messages(
            self.system_prompt,
            bot_name=bot_name,
            timezone_name=timezone_name,
            summary=summary,
            history=history,
            question=question,
        )

    async def complete(
        self,
        messages: list[Message],
        *,
        on_text: Callable[[str], None] | None = None,
        tools: ToolRegistry | None = None,
        on_event: EventHandler | None = None,
        parallel_tools: bool = True,
        timer: TurnTimer | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        quick: bool = False,
    ) -> str:
        """The model's answer. With `on_text`, it is streamed: called with the text so far.

        Text the model writes before calling a tool stays part of the answer.
        `model` and `max_tokens` override the configured ones for this call,
        as for a side job given to a cheaper model. `quick` marks such a side
        job: its request carries the configured extra fields for them.
        """
        messages = list(messages)
        said: list[str] = []
        extra = self.quick_extra_body if quick else None
        try:
            for tool_round in range(MAX_TOOL_ROUNDS + 1):
                if timer is not None:
                    timer.rounds += 1
                text, calls = await self._round(
                    messages, said, on_text, tools, timer, model, max_tokens, extra
                )
                if text.strip():
                    said.append(text.strip())
                if not calls:
                    return "\n\n".join(said)
                if tool_round == MAX_TOOL_ROUNDS:
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {"name": call.name, "arguments": call.arguments},
                            }
                            for call in calls
                        ],
                    }
                )
                assert tools is not None
                results = await self._run_tools(tools, calls, on_event, parallel_tools, timer)
                for call, result in zip(calls, results, strict=True):
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        except APITimeoutError:
            raise LLMError(
                f"the language model did not answer within {self.timeout_seconds:g}s"
            ) from None
        except APIConnectionError as exc:
            log.warning("Could not reach the language model: %s", exc)
            raise LLMError("could not reach the language model") from None
        except APIStatusError as exc:
            log.warning("Language model returned HTTP %s: %s", exc.status_code, exc.message)
            raise LLMError(
                f"the language model returned an error (HTTP {exc.status_code})"
            ) from None
        except APIError as exc:
            log.warning("Language model call failed: %s", exc)
            raise LLMError("the language model call failed") from None
        raise LLMError("the language model kept calling tools without answering")

    async def _run_tools(
        self,
        tools: ToolRegistry,
        calls: list[ToolCall],
        on_event: EventHandler | None,
        parallel: bool,
        timer: TurnTimer | None,
    ) -> list[str]:
        """Results of `calls`, in order; all at once when `parallel`."""

        async def run(call: ToolCall) -> str:
            if on_event is not None:
                on_event("tool_start", call.name)
            begin = time.monotonic()
            try:
                result = await asyncio.wait_for(tools.call(call), TOOL_TIMEOUT_SECONDS)
            except TimeoutError:
                result = tool_result(
                    {"error": f"the tool did not answer within {TOOL_TIMEOUT_SECONDS:g}s"}
                )
            ok = not is_error_result(result)
            if timer is not None:
                timer.tools.append(ToolTiming(call.name, (time.monotonic() - begin) * 1000, ok))
            if on_event is not None:
                on_event("tool_end", call.name, ok)
            return result

        begin = time.monotonic()
        try:
            if parallel and len(calls) > 1:
                return list(await asyncio.gather(*(run(call) for call in calls)))
            return [await run(call) for call in calls]
        finally:
            if timer is not None:
                # Wall time: tools that ran side by side count once.
                timer.add_wait("tools", time.monotonic() - begin)

    async def _round(
        self,
        messages: list[Message],
        said: list[str],
        on_text: Callable[[str], None] | None,
        tools: ToolRegistry | None,
        timer: TurnTimer | None,
        model: str | None,
        max_tokens: int | None,
        extra_body: dict[str, Any] | None = None,
    ) -> tuple[str, list[ToolCall]]:
        """One completion: its text, and the tools it wants called."""
        options: dict = {}
        if extra_body:
            options["extra_body"] = extra_body
        if max_tokens or self._max_tokens:
            options["max_tokens"] = max_tokens or self._max_tokens
        if self._temperature is not None:
            options["temperature"] = self._temperature
        if tools:
            options["tools"] = tools.schemas
        model = model or self.model

        if on_text is None:
            begin = time.monotonic()
            try:
                response = await self._client.chat.completions.create(
                    model=model, messages=messages, **options
                )
            finally:
                if timer is not None:
                    timer.add_wait("llm", time.monotonic() - begin)
            if timer is not None:
                timer.mark("llm_first_token")
            if not response.choices:
                return "", []
            message = response.choices[0].message
            calls = [
                ToolCall(call.id, call.function.name, call.function.arguments or "")
                for call in message.tool_calls or []
                if getattr(call, "function", None) is not None
            ]
            return message.content or "", calls

        parts: list[str] = []
        pending: dict[int, ToolCall] = {}
        waited = 0.0
        begin = time.monotonic()
        try:
            stream = await self._client.chat.completions.create(
                model=model, messages=messages, stream=True, **options
            )
            waited += time.monotonic() - begin
            async with stream:
                chunks = stream.__aiter__()
                while True:
                    begin = time.monotonic()
                    try:
                        chunk = await chunks.__anext__()
                    except StopAsyncIteration:
                        break
                    finally:
                        waited += time.monotonic() - begin
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if timer is not None and (delta.content or delta.tool_calls):
                        timer.mark("llm_first_token")
                    if delta.content:
                        parts.append(delta.content)
                        on_text("\n\n".join([*said, "".join(parts).lstrip()]))
                    # A tool call arrives in pieces: id and name first, then the arguments.
                    for piece in delta.tool_calls or []:
                        call = pending.setdefault(
                            piece.index, ToolCall(f"call_{piece.index}", "", "")
                        )
                        if piece.id:
                            call.id = piece.id
                        if piece.function is not None:
                            call.name = call.name or piece.function.name or ""
                            call.arguments += piece.function.arguments or ""
        finally:
            if timer is not None:
                timer.add_wait("llm", waited)
        return "".join(parts), [pending[index] for index in sorted(pending)]


def _shared_http_client() -> httpx.AsyncClient:
    """One connection pool for every call to the gateway.

    Connections are kept open between messages, so a question does not pay
    for a new TCP and TLS handshake, and HTTP/2 is used where the gateway
    offers it (when the `h2` package is installed).
    """
    try:
        import h2  # noqa: F401

        http2 = True
    except ImportError:
        http2 = False
    return httpx.AsyncClient(
        http2=http2,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=120),
        # The SDK sets its own timeouts per request.
        timeout=None,
    )


def _extra_body_or_none(text: str) -> dict[str, Any] | None:
    try:
        return parse_extra_body(text) or None
    except ValueError as exc:
        # Checked when saved; a bad value only loses the extra fields.
        log.warning("Ignoring the extra request fields for quick jobs: %s", exc)
        return None


class LLMService:
    """The language model as the bot sees it: built from the portal's settings.

    The client is rebuilt only when how to reach the model changes; every
    client shares one connection pool.
    """

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        # Tests hand in a client with a fake transport.
        self._http_client = http_client
        self._chat: LLMChat | None = None
        self._built_from: LLMSettings | None = None

    def chat_for(self, settings: LLMSettings) -> LLMChat | None:
        """The client for these settings; None if the model is not configured."""
        if settings != self._built_from:
            if self._http_client is None and settings.configured:
                self._http_client = _shared_http_client()
            self._chat = LLMChat.from_settings(settings, http_client=self._http_client)
            self._built_from = settings
        return self._chat

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
