"""Tools of MCP servers, offered to the model as `mcp__<server>__<tool>`.

Only tools an admin turned on, of servers that are on, are offered; the
tool policy (`app.agent.policy`) then decides who gets them, except that a
tool the admin opened to everyone goes to everyone. A tool with
`cache_seconds` has its answers reused for the same arguments. A failure is
a result the model reads, never an exception.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable
from typing import Any

from app.agent.tools import tool_result
from app.cache.ttl import TTLCache
from app.integrations import mcp
from app.integrations.mcp import McpError, McpToolSpec

log = logging.getLogger(__name__)

# The registry keeps tool names to 60 characters (`app.agent.tools.tool_name`).
MAX_NAME = 60


def mcp_tool_name(server: str, tool: str) -> str:
    def clean(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", text)

    return f"mcp__{clean(server)}__{clean(tool)}"[:MAX_NAME]


class McpTools:
    def __init__(self, specs: Iterable[McpToolSpec], *, cache: TTLCache | None = None) -> None:
        # Shared with integrations' reusable answers (`AgentRuntime.tool_results`).
        self._cache = cache
        self._specs: dict[str, McpToolSpec] = {}
        for spec in specs:
            self._specs.setdefault(mcp_tool_name(spec.server.name, spec.name), spec)

    def tools(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "description": (f"[{spec.server.name}] {spec.description}".strip())[:1024],
                "parameters": spec.schema(),
            }
            for name, spec in self._specs.items()
        }

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        spec = self._specs[name]
        key = None
        if self._cache is not None and spec.cache_seconds > 0:
            # The server's settings are part of the key: new headers, new answers.
            key = ("mcp", spec.server, spec.name, json.dumps(arguments, sort_keys=True, default=str))
            kept = self._cache.get(key)
            if kept is not None:
                log.info("MCP tool %s answered (reused)", name)
                return kept
        started = time.monotonic()
        try:
            ok, text = await mcp.call_tool(spec.server, spec.name, arguments, read_only=spec.read_only)
        except McpError as exc:
            log.info("MCP tool %s failed: %s", name, exc)
            return tool_result({"error": str(exc)})
        log.info(
            "MCP tool %s %s in %d ms",
            name,
            "answered" if ok else "returned an error",
            (time.monotonic() - started) * 1000,
        )
        if not ok:
            return tool_result({"error": text or "the tool failed"})
        result = tool_result({"result": text})
        if key is not None:
            self._cache.put(key, result, spec.cache_seconds)  # type: ignore[union-attr]
        return result
