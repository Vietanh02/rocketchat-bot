"""MCP servers whose tools the model may call, and which of those tools are on."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent.tools.mcp import mcp_tool_name
from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.bot.config_store import bump_config_version
from app.integrations import mcp
from app.integrations.mcp import McpError, McpServerSpec
from app.models import McpServer, McpTool, McpTransport
from app.schemas import (
    McpCallIn,
    McpCallOut,
    McpServerIn,
    McpServerOut,
    McpServerUpdate,
    McpToolOut,
    McpToolUpdate,
)
from app.security import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

PLAIN_FIELDS = (
    "name",
    "description",
    "transport",
    "url",
    "command",
    "timeout_seconds",
    "keep_alive_seconds",
    "enabled",
)


def _names(ciphertext: str | None) -> list[str]:
    plain = decrypt_secret(ciphertext) if ciphertext else None
    try:
        value = json.loads(plain) if plain else {}
    except ValueError:
        return []
    return sorted(value) if isinstance(value, dict) else []


def _out(server: McpServer) -> McpServerOut:
    return McpServerOut(
        id=server.id,
        name=server.name,
        description=server.description or "",
        transport=server.transport,  # type: ignore[arg-type]
        url=server.url or "",
        command=server.command or "",
        args=[str(a) for a in json.loads(server.args_json or "[]")],
        timeout_seconds=server.timeout_seconds,
        keep_alive_seconds=server.keep_alive_seconds or 0,
        enabled=server.enabled,
        connected=server.id in mcp.pool.open_servers(),
        header_names=_names(server.headers_enc),
        env_names=_names(server.env_enc),
        last_error=server.last_error,
        tools_refreshed_at=server.tools_refreshed_at,
        tools=[
            McpToolOut(
                id=tool.id,
                name=tool.name,
                tool_name=mcp_tool_name(server.name, tool.name),
                description=tool.description or "",
                input_schema=json.loads(tool.input_schema_json or "{}"),
                enabled=tool.enabled,
                everyone=tool.everyone,
                read_only=tool.read_only,
                destructive=tool.destructive,
                cache_seconds=tool.cache_seconds or 0,
            )
            for tool in server.tools
        ],
    )


def _secret(value: dict[str, str] | None) -> str | None:
    return encrypt_secret(json.dumps(value)) if value else None


async def _load(session, server_id: int) -> McpServer:
    server = await session.get(
        McpServer, server_id, options=[selectinload(McpServer.tools)], populate_existing=True
    )
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such MCP server")
    return server


def _check(server: McpServer) -> None:
    if server.transport == McpTransport.http.value and not server.url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="An HTTP server needs a URL")
    if server.transport == McpTransport.stdio.value and not server.command:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="A stdio server needs a command")


async def _changed(session, runtime) -> None:
    await bump_config_version(session)
    await session.commit()
    await runtime.apply_config_change()


@router.get("", response_model=list[McpServerOut])
async def list_servers(_: CurrentUser, session: SessionDep) -> list[McpServerOut]:
    servers = (
        await session.scalars(select(McpServer).options(selectinload(McpServer.tools)).order_by(McpServer.name))
    ).all()
    return [_out(server) for server in servers]


@router.post("", response_model=McpServerOut, status_code=status.HTTP_201_CREATED)
async def create_server(
    payload: McpServerIn, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> McpServerOut:
    if await session.scalar(select(McpServer.id).where(McpServer.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"A server named {payload.name!r} exists")
    server = McpServer(
        **{field: getattr(payload, field) for field in PLAIN_FIELDS},
        args_json=json.dumps(payload.args),
        headers_enc=_secret(payload.headers),
        env_enc=_secret(payload.env),
    )
    _check(server)
    session.add(server)
    await session.flush()
    record_audit(session, admin, "create", "mcp_server", server.id, server.name)
    await _changed(session, runtime)
    return _out(await _load(session, server.id))


@router.put("/{server_id}", response_model=McpServerOut)
async def update_server(
    server_id: int, payload: McpServerUpdate, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> McpServerOut:
    server = await _load(session, server_id)
    changes = payload.model_dump(exclude_unset=True)
    for field in PLAIN_FIELDS:
        if changes.get(field) is not None:
            setattr(server, field, changes[field])
    if changes.get("args") is not None:
        server.args_json = json.dumps(changes["args"])
    if "headers" in changes and changes["headers"] is not None:
        server.headers_enc = _secret(changes["headers"])
    if "env" in changes and changes["env"] is not None:
        server.env_enc = _secret(changes["env"])
    _check(server)
    record_audit(session, admin, "update", "mcp_server", server.id, ", ".join(sorted(changes)) or "(no changes)")
    await _changed(session, runtime)
    mcp.pool.forget(server_id)
    return _out(await _load(session, server_id))


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(server_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep) -> None:
    server = await _load(session, server_id)
    record_audit(session, admin, "delete", "mcp_server", server.id, server.name)
    await session.delete(server)
    await _changed(session, runtime)
    mcp.pool.forget(server_id)


@router.post("/{server_id}/refresh", response_model=McpServerOut)
async def refresh_tools(
    server_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> McpServerOut:
    """Ask the server for its tools; new ones start off, removed ones go.
    What the server says of each (read-only, destructive) is kept."""
    server = await _load(session, server_id)
    try:
        listed = await mcp.list_tools(McpServerSpec.from_model(server))
    except McpError as exc:
        server.last_error = str(exc)[:1000]
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    known = {tool.name: tool for tool in server.tools}
    keep = []
    for item in listed:
        tool = known.get(item.name)
        if tool is None:
            tool = McpTool(name=item.name, enabled=False)
        tool.description = item.description
        tool.input_schema_json = json.dumps(item.input_schema)
        tool.read_only = item.read_only
        tool.destructive = item.destructive
        keep.append(tool)
    server.tools = keep
    server.last_error = None
    server.tools_refreshed_at = datetime.now(timezone.utc)
    record_audit(session, admin, "update", "mcp_server", server.id, f"refreshed: {len(keep)} tools")
    await _changed(session, runtime)
    return _out(await _load(session, server_id))


@router.put("/{server_id}/tools/{tool_id}", response_model=McpServerOut)
async def set_tool(
    server_id: int,
    tool_id: int,
    payload: McpToolUpdate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> McpServerOut:
    tool = await session.get(McpTool, tool_id)
    if tool is None or tool.server_id != server_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such tool")
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(tool, field, value)
    summary = ", ".join(f"{field}={value}" for field, value in sorted(changes.items())) or "(no changes)"
    record_audit(session, admin, "update", "mcp_tool", tool.id, f"{tool.name}: {summary}")
    await _changed(session, runtime)
    return _out(await _load(session, server_id))


@router.post("/{server_id}/tools/{tool_id}/call", response_model=McpCallOut)
async def call_tool(
    server_id: int, tool_id: int, payload: McpCallIn, _: AdminUser, session: SessionDep
) -> McpCallOut:
    """Try a tool with these arguments (even one that is off)."""
    server = await _load(session, server_id)
    tool = next((t for t in server.tools if t.id == tool_id), None)
    if tool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such tool")
    started = time.monotonic()
    try:
        ok, text = await mcp.call_tool(
            McpServerSpec.from_model(server), tool.name, payload.arguments, read_only=tool.read_only
        )
    except McpError as exc:
        ok, text = False, str(exc)
    return McpCallOut(ok=ok, text=text, duration_ms=int((time.monotonic() - started) * 1000))
