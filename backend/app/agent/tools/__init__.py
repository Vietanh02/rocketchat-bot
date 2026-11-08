"""What the model may call, gathered from several providers.

A provider (the integrations today; MCP servers, memory and skills later)
offers OpenAI-style function schemas and runs calls by name. The registry
gives every tool a unique name and sends each call to its provider. A
failure is a result the model reads, never an exception.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

# A tool's result is clipped to this before the model sees it.
MAX_TOOL_RESULT_CHARS = 8000


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


class ToolProvider(Protocol):
    """Tools of one kind. `schemas()` names them; `call()` runs one by that name."""

    def tools(self) -> dict[str, dict[str, Any]]:
        """{tool name: OpenAI function schema without the name}."""
        ...

    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...


class ToolRegistry:
    """Every tool offered to the model in one turn."""

    def __init__(self, providers: Iterable[ToolProvider] = ()) -> None:
        self._routes: dict[str, tuple[ToolProvider, str]] = {}
        self.schemas: list[dict[str, Any]] = []
        for provider in providers:
            for own_name, function in provider.tools().items():
                name = tool_name(own_name, self._routes)
                self._routes[name] = (provider, own_name)
                self.schemas.append({"type": "function", "function": {**function, "name": name}})

    def __bool__(self) -> bool:
        return bool(self._routes)

    def __len__(self) -> int:
        return len(self._routes)

    @property
    def names(self) -> list[str]:
        return list(self._routes)

    async def call(self, call: ToolCall) -> str:
        """The result as JSON for the model; failures are results too, never raised."""
        route = self._routes.get(call.name)
        if route is None:
            return tool_result({"error": f"there is no tool named {call.name!r}"})
        try:
            arguments = json.loads(call.arguments or "{}")
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            return tool_result({"error": "arguments must be a JSON object"})
        provider, own_name = route
        return await provider.call(own_name, arguments)


def tool_name(name: str, taken: dict[str, Any]) -> str:
    """`name` as a valid, unused function name (letters, digits, _ and -)."""
    base = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:60] or "tool"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def tool_result(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + " …(truncated)"
    return text


def is_error_result(result: str) -> bool:
    return result.startswith('{"error"')
