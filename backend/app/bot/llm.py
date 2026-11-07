"""Replies from a language model behind an OpenAI-compatible API.

Used when `bot_config.responder_mode` is "llm": commands still answer first,
and whatever matches none of them is sent to the model instead of getting the
fallback reply. The gateway, key, model and timeout are set in the portal
(`llm_config`); `LLMService` rebuilds its client whenever they change.

Each conversation (a DM, or a thread or channel) keeps its last few turns in
memory, so the model can follow up on what was just said. That memory is lost
on restart, which is fine for a chat assistant.

Integrations marked "LLM tool" in the portal are offered to the model as
functions. When it calls one, the request goes through the same parameter
binding and SSRF guard as a command would, and the response is handed back
to the model, which then answers (or calls another tool).
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from app.bot.config_store import LLMSettings
from app.integrations.executor import (
    IntegrationError,
    IntegrationSpec,
    bind_params,
    execute_integration,
)

log = logging.getLogger(__name__)

_PROMPT_START = (
    "You are {bot}, a helpful assistant chatting with people in Rocket.Chat. "
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
# Conversations remembered at once; the least recently used are forgotten.
MAX_CONVERSATIONS = 500
# Rounds of tool calls in one answer before the model is cut off.
MAX_TOOL_ROUNDS = 5
# An integration's response is clipped to this before the model sees it.
MAX_TOOL_RESULT_CHARS = 8000

Message = dict[str, Any]
ConversationKey = tuple[str, str | None]


class LLMError(Exception):
    """The model could not answer; the message is safe to show in chat."""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


class Toolbox:
    """The integrations offered to the model, callable by their tool names."""

    def __init__(self, specs: Iterable[IntegrationSpec], context: dict[str, Any]) -> None:
        # What integration templates see, as when a command calls them.
        self._context = context
        self._specs: dict[str, IntegrationSpec] = {}
        for spec in specs:
            self._specs[_tool_name(spec.name, self._specs)] = spec
        self.schemas = [spec.to_tool_schema(name) for name, spec in self._specs.items()]

    def __bool__(self) -> bool:
        return bool(self._specs)

    async def call(self, call: ToolCall) -> str:
        """The result as JSON for the model; failures are results too, never raised."""
        spec = self._specs.get(call.name)
        if spec is None:
            return _tool_result({"error": f"there is no tool named {call.name!r}"})
        try:
            arguments = json.loads(call.arguments or "{}")
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            return _tool_result({"error": "arguments must be a JSON object"})
        named = {
            str(key): value if isinstance(value, str) else json.dumps(value)
            for key, value in arguments.items()
            if value is not None
        }
        try:
            params = bind_params(spec.params, [], named=named)
            result = await execute_integration(spec, params=params, base_context=self._context)
        except IntegrationError as exc:
            log.info("LLM tool %r failed: %s", spec.name, exc)
            return _tool_result({"error": str(exc)})
        log.info("LLM tool %r returned HTTP %s", spec.name, result.status_code)
        if not result.ok:
            return _tool_result({"error": result.error, "response": result.data})
        response = result.data if result.data is not None else result.text
        return _tool_result({"status": result.status_code, "response": response})


def _tool_name(name: str, taken: dict[str, Any]) -> str:
    """`name` as a valid, unused function name (letters, digits, _ and -)."""
    base = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:60] or "tool"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def _tool_result(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + " …(truncated)"
    return text


class ConversationMemory:
    """The last few turns of each conversation, oldest first."""

    def __init__(self, max_messages: int, max_conversations: int = MAX_CONVERSATIONS) -> None:
        self._max_messages = max_messages
        self._max_conversations = max_conversations
        self._turns: OrderedDict[ConversationKey, deque[Message]] = OrderedDict()

    @property
    def max_messages(self) -> int:
        return self._max_messages

    def resize(self, max_messages: int) -> None:
        """Keep this many messages per conversation from now on."""
        self._max_messages = max(0, max_messages)
        if self._max_messages == 0:
            self._turns.clear()

    def history(self, key: ConversationKey) -> list[Message]:
        if self._max_messages <= 0:
            return []
        return list(self._turns.get(key, ()))[-self._max_messages :]

    def remember(self, key: ConversationKey, question: str, answer: str) -> None:
        if self._max_messages <= 0:
            return
        turns = self._turns.pop(key, None)
        if turns is None or turns.maxlen != self._max_messages:
            turns = deque(turns or (), maxlen=self._max_messages)
        turns.append({"role": "user", "content": question})
        turns.append({"role": "assistant", "content": answer})
        self._turns[key] = turns
        while len(self._turns) > self._max_conversations:
            self._turns.popitem(last=False)


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
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
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
            http_client=http_client,
        )

    async def aclose(self) -> None:
        await self._client.close()

    def build_messages(
        self, question: str, history: list[Message], *, bot_name: str
    ) -> list[Message]:
        system = self.system_prompt.replace("{bot}", bot_name)
        return [{"role": "system", "content": system}, *history, {"role": "user", "content": question}]

    async def complete(
        self,
        messages: list[Message],
        *,
        on_text: Callable[[str], None] | None = None,
        tools: Toolbox | None = None,
    ) -> str:
        """The model's answer. With `on_text`, it is streamed: called with the text so far.

        Text the model writes before calling a tool stays part of the answer.
        """
        messages = list(messages)
        said: list[str] = []
        try:
            for tool_round in range(MAX_TOOL_ROUNDS + 1):
                text, calls = await self._round(messages, said, on_text, tools)
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
                for call in calls:
                    assert tools is not None
                    result = await tools.call(call)
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

    async def _round(
        self,
        messages: list[Message],
        said: list[str],
        on_text: Callable[[str], None] | None,
        tools: Toolbox | None,
    ) -> tuple[str, list[ToolCall]]:
        """One completion: its text, and the tools it wants called."""
        options: dict = {}
        if self._max_tokens:
            options["max_tokens"] = self._max_tokens
        if self._temperature is not None:
            options["temperature"] = self._temperature
        if tools:
            options["tools"] = tools.schemas

        if on_text is None:
            response = await self._client.chat.completions.create(
                model=self.model, messages=messages, **options
            )
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
        stream = await self._client.chat.completions.create(
            model=self.model, messages=messages, stream=True, **options
        )
        async with stream:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
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
        return "".join(parts), [pending[index] for index in sorted(pending)]


class LLMService:
    """The language model as the bot sees it: built from the portal's settings.

    The client is rebuilt only when how to reach the model changes, and the
    conversations are kept across such changes.
    """

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        # Tests hand in a client with a fake transport.
        self._http_client = http_client
        self._chat: LLMChat | None = None
        self._built_from: LLMSettings | None = None
        self.memory = ConversationMemory(10)

    def chat_for(self, settings: LLMSettings) -> LLMChat | None:
        """The client for these settings; None if the model is not configured."""
        self.memory.resize(settings.history_messages)
        if settings != self._built_from:
            self._chat = LLMChat.from_settings(settings, http_client=self._http_client)
            self._built_from = settings
        return self._chat
