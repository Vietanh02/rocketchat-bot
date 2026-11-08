"""Integrations marked "LLM tool" in the portal, offered to the model.

A call goes through the same parameter binding and SSRF guard as a command
calling the integration would, and the response is handed back to the model.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from app.agent.tools import ToolRegistry, tool_name, tool_result
from app.integrations.executor import (
    IntegrationError,
    IntegrationSpec,
    bind_params,
    execute_integration,
)

log = logging.getLogger(__name__)


class IntegrationTools:
    """One tool per integration; arguments are bound like a command's parameters."""

    def __init__(self, specs: Iterable[IntegrationSpec], context: dict[str, Any]) -> None:
        # What integration templates see, as when a command calls them.
        self._context = context
        self._specs: dict[str, IntegrationSpec] = {}
        for spec in specs:
            # Integration names may hold characters function names do not allow.
            self._specs[tool_name(spec.name, self._specs)] = spec

    def tools(self) -> dict[str, dict[str, Any]]:
        return {
            name: spec.to_tool_schema(name)["function"] for name, spec in self._specs.items()
        }

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        spec = self._specs[name]
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
            return tool_result({"error": str(exc)})
        log.info("LLM tool %r returned HTTP %s", spec.name, result.status_code)
        if not result.ok:
            return tool_result({"error": result.error, "response": result.data})
        response = result.data if result.data is not None else result.text
        return tool_result({"status": result.status_code, "response": response})


def Toolbox(specs: Iterable[IntegrationSpec], context: dict[str, Any]) -> ToolRegistry:  # noqa: N802
    """The integrations alone as a registry (the shape older callers expect)."""
    return ToolRegistry([IntegrationTools(specs, context)])
