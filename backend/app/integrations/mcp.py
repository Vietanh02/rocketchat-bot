"""The bot as an MCP client: list and call the tools of MCP servers.

Two transports (`app.models.McpTransport`):

- **http** (Streamable HTTP): a URL and headers. The URL goes through the
  same private-network guard as integrations; redirects are not followed.
- **stdio**: a command started on this machine (in the backend container),
  spoken to over its stdin/stdout. Only an admin can set one up. It runs in
  the MCP servers folder (`APP_MCP_SERVERS_DIR`), and `python` there means
  the bot's own interpreter, so a Python server kept in that folder can use
  every package the bot has (the `mcp` SDK among them).

Calls go through `pool`: a server's session stays open for the next call
until it has been idle for the server's `keep_alive_seconds` (0: a new
session per call). That saves starting the process of a stdio server, and
the handshake, on every call. A session idle for a while is pinged before
use, and one that fails is dropped, so a restarted server is reconnected.
Listing tools (a refresh in the portal) always opens a new session.

Everything is bounded by the server's timeout, and results are clipped
like any tool result.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.integrations.executor import IntegrationError, assert_url_allowed
from app.models import McpServer, McpTransport
from app.net import describe_error, httpx_verify, proxy_env
from app.security import decrypt_secret

log = logging.getLogger(__name__)

CLIENT_NAME = "rocketchat-bot"
# Text taken from one result before it is clipped again by the registry.
MAX_RESULT_CHARS = 20_000
# A session idle longer than this is pinged before it is used again.
PING_AFTER_IDLE_SECONDS = 30.0
# How often idle sessions are looked for.
REAP_EVERY_SECONDS = 15.0
# How long closing a session (and its process) may take.
CLOSE_SECONDS = 5.0


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
    keep_alive_seconds: float = 0.0

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
            keep_alive_seconds=float(model.keep_alive_seconds or 0),
        )


@dataclass(slots=True, frozen=True)
class McpToolSpec:
    """An enabled tool of an enabled server, as offered to the model."""

    server: McpServerSpec
    name: str
    description: str = ""
    input_schema: str = "{}"
    # Offered to everyone, whatever the policy's globs say.
    everyone: bool = False
    # The server says calling it changes nothing: safe to retry and to cache.
    read_only: bool = False
    # Answers reused for the same arguments for this long.
    cache_seconds: int = 0

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
    read_only: bool = False
    destructive: bool = False


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


def servers_dir() -> Path | None:
    """Where stdio servers run, if that folder exists."""
    folder = Path(get_settings().mcp_servers_dir)
    return folder if folder.is_dir() else None


def stdio_command(command: str) -> str:
    """`python` is the bot's own interpreter, which has the `mcp` SDK."""
    return sys.executable if command in ("python", "python3") else command


# Builds the client for a server; tests replace it to talk to one in-process.
ClientFactory = Callable[[McpServerSpec], Any]


def _default_client(spec: McpServerSpec):
    from mcp import Client, StdioServerParameters

    info = _client_info()
    if spec.transport == McpTransport.stdio.value:
        if not spec.command:
            raise McpError("no command is set")
        folder = servers_dir()
        # The SDK passes on only HOME, PATH and the like; the proxy settings
        # go too, with the server's own variables over them. Output is not
        # buffered, so a Python server's answers are not held back.
        env = {"PYTHONUNBUFFERED": "1", **proxy_env(), **dict(spec.env)}
        params = StdioServerParameters(
            command=stdio_command(spec.command),
            args=list(spec.args),
            env=env,
            cwd=str(folder) if folder else None,
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
        verify=httpx_verify(),
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


# --------------------------------------------------------------------------- #
# Kept-open sessions
# --------------------------------------------------------------------------- #


class _Connection:
    """One open session. Its own task enters and leaves the client, as the
    SDK's task groups must be left by the task that entered them; callers
    only send requests on it."""

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.client: Any = None
        self.in_use = 0
        self.last_used = time.monotonic()
        self.retired = False
        self._ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name=f"mcp-session:{spec.name}")

    async def _run(self) -> None:
        try:
            async with client_factory(self.spec) as client:
                self.client = client
                self._ready.set_result(client)
                await self._stop.wait()
        except asyncio.CancelledError:
            if not self._ready.done():
                self._ready.cancel()
            raise
        except BaseException as exc:  # noqa: BLE001 - reported to whoever waits
            if not self._ready.done():
                self._ready.set_exception(exc)
            else:
                log.info("MCP %s: session ended: %s", self.spec.name, describe_error(exc))
        finally:
            self.client = None

    async def ready(self) -> Any:
        return await asyncio.shield(self._ready)

    @property
    def alive(self) -> bool:
        return self.client is not None and not self._task.done() and not self.retired

    async def close(self) -> None:
        self.retired = True
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), CLOSE_SECONDS)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        except BaseException:  # noqa: BLE001 - it has ended either way
            pass


class McpPool:
    """Open sessions by server id, closed once idle (`keep_alive_seconds`)."""

    def __init__(self) -> None:
        self._connections: dict[int, _Connection] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._closing: set[asyncio.Task] = set()
        self._reaper: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def __len__(self) -> int:
        return len(self._connections)

    def _same_loop(self) -> None:
        # A pool used from another event loop (tests) starts afresh.
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._connections.clear()
            self._locks.clear()
            self._closing.clear()
            self._reaper = None
            self._loop = loop

    def _retire(self, conn: _Connection) -> None:
        """No new calls on it; closed once the ones running are done."""
        conn.retired = True
        if self._connections.get(conn.spec.id) is conn:
            del self._connections[conn.spec.id]
        if conn.in_use == 0:
            task = asyncio.create_task(conn.close())
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    async def _acquire(self, spec: McpServerSpec) -> tuple[_Connection, bool]:
        """A session for this server: (it, whether it was just opened)."""
        lock = self._locks.setdefault(spec.id, asyncio.Lock())
        async with lock:
            conn = self._connections.get(spec.id)
            if conn is not None and (conn.spec != spec or not conn.alive):
                self._retire(conn)
                conn = None
            if conn is not None and time.monotonic() - conn.last_used > PING_AFTER_IDLE_SECONDS:
                try:
                    async with asyncio.timeout(min(5.0, spec.timeout_seconds)):
                        await conn.client.send_ping()
                except Exception as exc:  # noqa: BLE001 - reopened below
                    log.info("MCP %s: idle session gone (%s), reopening", spec.name, describe_error(exc))
                    self._retire(conn)
                    conn = None
            fresh = conn is None
            if conn is None:
                conn = _Connection(spec)
                try:
                    await conn.ready()
                except BaseException:
                    self._retire(conn)
                    raise
                self._connections[spec.id] = conn
                self._start_reaper()
            conn.in_use += 1
            return conn, fresh

    def _release(self, conn: _Connection) -> None:
        conn.in_use -= 1
        conn.last_used = time.monotonic()
        if conn.retired and conn.in_use == 0:
            self._retire(conn)

    async def call(self, spec: McpServerSpec, name: str, arguments: dict[str, Any], *, retry: bool):
        """Call a tool on a kept session. A session that fails is dropped; when
        it had been open a while and `retry` (the tool changes nothing), the
        call is made once more on a new one."""
        self._same_loop()
        conn, fresh = await self._acquire(spec)
        try:
            return await conn.client.call_tool(name, arguments)
        except BaseException as exc:
            self._retire(conn)
            if fresh or not retry or not isinstance(exc, Exception):
                raise
            log.info("MCP %s: %s failed on a kept session (%s), trying a new one", spec.name, name, describe_error(exc))
        finally:
            self._release(conn)
        conn, _ = await self._acquire(spec)
        try:
            return await conn.client.call_tool(name, arguments)
        except BaseException:
            self._retire(conn)
            raise
        finally:
            self._release(conn)

    def _start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap(), name="mcp-session-reaper")

    async def _reap(self) -> None:
        while self._connections:
            await asyncio.sleep(REAP_EVERY_SECONDS)
            now = time.monotonic()
            for conn in list(self._connections.values()):
                if conn.in_use == 0 and now - conn.last_used >= conn.spec.keep_alive_seconds:
                    log.debug("MCP %s: closing the idle session", conn.spec.name)
                    self._retire(conn)

    def forget(self, server_id: int) -> None:
        """Close a server's session (its settings changed, or it was removed)."""
        conn = self._connections.get(server_id)
        if conn is not None and self._loop is asyncio.get_running_loop():
            self._retire(conn)

    def open_servers(self) -> list[int]:
        return [sid for sid, conn in self._connections.items() if conn.alive]

    async def close_all(self) -> None:
        if self._loop is not asyncio.get_running_loop():
            return
        for conn in list(self._connections.values()):
            self._retire(conn)
        if self._reaper is not None:
            self._reaper.cancel()
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)


pool = McpPool()


# --------------------------------------------------------------------------- #
# Listing and calling
# --------------------------------------------------------------------------- #


async def _guard(spec: McpServerSpec) -> None:
    if spec.transport == McpTransport.http.value and spec.url:
        try:
            await assert_url_allowed(spec.url)
        except IntegrationError as exc:
            raise McpError(str(exc)) from exc


def _describe(exc: BaseException) -> str:
    return describe_error(exc)


def _listed(tool) -> ListedTool:
    hints = getattr(tool, "annotations", None)
    return ListedTool(
        tool.name,
        (tool.description or tool.title or "").strip(),
        dict(tool.input_schema or {}),
        read_only=bool(getattr(hints, "read_only_hint", False)),
        # The protocol's default is "may destroy" unless it says read-only.
        destructive=bool(hints is not None and getattr(hints, "destructive_hint", False)),
    )


async def list_tools(spec: McpServerSpec) -> list[ListedTool]:
    """Every tool the server offers (on a new session, to check the settings)."""
    await _guard(spec)
    try:
        async with asyncio.timeout(spec.timeout_seconds):
            async with client_factory(spec) as client:
                tools: list[ListedTool] = []
                cursor = None
                for _ in range(20):  # pages
                    result = await client.list_tools(cursor=cursor)
                    tools.extend(_listed(tool) for tool in result.tools)
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


async def call_tool(
    spec: McpServerSpec, name: str, arguments: dict[str, Any], *, read_only: bool = False
) -> tuple[bool, str]:
    """Call one tool: (ok, its text). McpError if the server could not be used.

    `read_only` (the server says the tool changes nothing) lets a call that
    failed on a kept session be made again on a new one."""
    await _guard(spec)
    try:
        async with asyncio.timeout(spec.timeout_seconds):
            if spec.keep_alive_seconds > 0:
                result = await pool.call(spec, name, arguments, retry=read_only)
            else:
                async with client_factory(spec) as client:
                    result = await client.call_tool(name, arguments)
    except McpError:
        raise
    except TimeoutError:
        raise McpError(f"{spec.name} did not answer within {spec.timeout_seconds:g}s") from None
    except Exception as exc:  # noqa: BLE001 - anything the transport raises
        raise McpError(f"calling {name} on {spec.name} failed: {_describe(exc)}") from None
    return result_text(result)

