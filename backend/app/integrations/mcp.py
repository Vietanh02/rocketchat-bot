"""The bot as an MCP client: list and call the tools of MCP servers.

Two transports (`app.models.McpTransport`):

- **http** (Streamable HTTP): a URL and headers. The URL goes through the
  same private-network guard as integrations; redirects are not followed.
- **stdio**: a command started on this machine (in the backend container),
  spoken to over its stdin/stdout. Only an admin can set one up, and the
  image must have what the command needs (`node`/`npx`, `uvx`, ...).

A new session is opened for each listing or call: simpler than keeping
sessions alive, and a server restart never leaves a broken one behind.
Everything is bounded by the server's timeout, and results are clipped
like any tool result.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.integrations.executor import IntegrationError, assert_url_allowed
from app.models import McpServer, McpTransport
from app.security import decrypt_secret

log = logging.getLogger(__name__)

CLIENT_NAME = "rocketchat-bot"
# Text taken from one result before it is clipped again by the registry.
MAX_RESULT_CHARS = 20_000


class McpError(RuntimeError):
    """The server could not be reached or refused; the message is safe to show."""


@dataclass(slots=True, frozen=True)
class McpServerSpec:
    id: int
    name: str
    transport: str
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    timeout_seconds: float = 30.0

    @classmethod
    def from_model(cls, model: McpServer) -> McpServerSpec:
        return cls(
            id=model.id,
            name=model.name,
            transport=model.transport,
            url=(model.url or "").strip(),
            headers=tuple(sorted(_secret_object(model.headers_enc).items())),
            command=(model.command or "").strip(),
            args=tuple(str(a) for a in _json_list(model.args_json)),
            env=tuple(sorted(_secret_object(model.env_enc).items())),
            timeout_seconds=model.timeout_seconds or 30.0,
        )


@dataclass(slots=True, frozen=True)
class McpToolSpec:
    """An enabled tool of an enabled server, as offered to the model."""

    server: McpServerSpec
    name: str
    description: str = ""
    input_schema: str = "{}"

    def schema(self) -> dict[str, Any]:
        try:
            value = json.loads(self.input_schema or "{}")
        except ValueError:
            value = {}
        if not isinstance(value, dict) or value.get("type") != "object":
            return {"type": "object", "properties": {}}
        return value


@dataclass(slots=True)
class ListedTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


def _json_list(raw: str | None) -> list:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _secret_object(ciphertext: str | None) -> dict[str, str]:
    plain = decrypt_secret(ciphertext) if ciphertext else None
    if not plain:
        return {}
    try:
        value = json.loads(plain)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


# Builds the client for a server; tests replace it to talk to one in-process.
ClientFactory = Callable[[McpServerSpec], Any]


def _default_client(spec: McpServerSpec):
    from mcp import Client, StdioServerParameters

    info = _client_info()
    if spec.transport == McpTransport.stdio.value:
        if not spec.command:
            raise McpError("no command is set")
        params = StdioServerParameters(
            command=spec.command, args=list(spec.args), env=dict(spec.env) or None
        )
        return Client(params, read_timeout_seconds=spec.timeout_seconds, client_info=info, cache=None)
    if not spec.url:
        raise McpError("no URL is set")
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    http = httpx2.AsyncClient(
        headers=dict(spec.headers),
        timeout=spec.timeout_seconds,
        follow_redirects=False,
    )
    return Client(
        streamable_http_client(spec.url, http_client=http),
        read_timeout_seconds=spec.timeout_seconds,
        client_info=info,
        cache=None,
    )


def _client_info():
    from mcp import Implementation

    return Implementation(name=CLIENT_NAME, version="1")


client_factory: ClientFactory = _default_client


async def _guard(spec: McpServerSpec) -> None:
    if spec.transport == McpTransport.http.value and spec.url:
        try:
            await assert_url_allowed(spec.url)
        except IntegrationError as exc:
            raise McpError(str(exc)) from exc


def _describe(exc: BaseException) -> str:
    # anyio groups hide the cause one level down.
    inner = getattr(exc, "exceptions", None)
    if inner:
        return _describe(inner[0])
    text = str(exc).strip()
    return text or exc.__class__.__name__


async def list_tools(spec: McpServerSpec) -> list[ListedTool]:
    """Every tool the server offers."""
    await _guard(spec)
    try:
        async with asyncio.timeout(spec.timeout_seconds):
            async with client_factory(spec) as client:
                tools: list[ListedTool] = []
                cursor = None
                for _ in range(20):  # pages
                    result = await client.list_tools(cursor=cursor)
                    for tool in result.tools:
                        tools.append(
                            ListedTool(
                                tool.name,
                                (tool.description or tool.title or "").strip(),
                                dict(tool.input_schema or {}),
                            )
                        )
                    cursor = result.next_cursor
                    if not cursor:
                        break
                return tools
    except McpError:
        raise
    except TimeoutError:
        raise McpError(f"{spec.name} did not answer within {spec.timeout_seconds:g}s") from None
    except Exception as exc:  # noqa: BLE001 - anything the transport raises
        raise McpError(f"could not list the tools of {spec.name}: {_describe(exc)}") from None


def result_text(result) -> tuple[bool, str]:
    """(ok, text) of a CallToolResult: its text parts, else its structured content."""
    parts: list[str] = []
    for item in result.content or []:
        kind = getattr(item, "type", "")
        if kind == "text":
            parts.append(item.text)
        elif kind == "resource":
            resource = getattr(item, "resource", None)
            text = getattr(resource, "text", None)
            parts.append(text if text else f"[resource {getattr(resource, 'uri', '')}]")
        elif kind:
            parts.append(f"[{kind}]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False, default=str))
    text = "\n".join(parts).strip()
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " …(truncated)"
    return not bool(result.is_error), text


async def call_tool(spec: McpServerSpec, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Call one tool: (ok, its text). McpError if the server could not be used."""
    await _guard(spec)
    try:
        async with asyncio.timeout(spec.timeout_seconds):
            async with client_factory(spec) as client:
                result = await client.call_tool(name, arguments)
    except McpError:
        raise
    except TimeoutError:
        raise McpError(f"{spec.name} did not answer within {spec.timeout_seconds:g}s") from None
    except Exception as exc:  # noqa: BLE001 - anything the transport raises
        raise McpError(f"calling {name} on {spec.name} failed: {_describe(exc)}") from None
    return result_text(result)
