"""An example MCP server in Python, spoken to over stdio.

Set it up on the portal's MCP page:

    transport  stdio
    command    python
    arguments  _example/server.py

`python` is the bot's own interpreter, so the `mcp` SDK (and everything
else the bot has, such as httpx) can be imported here. The server runs in
the MCP servers folder (/app/mcp_servers in the container).

Raise ToolError for a failure the model should read: other exceptions reach
it only as "Error executing tool ...". Tools marked read-only tell the bot
that calling them changes nothing: the portal can then offer them to
everyone and reuse their answers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

server = MCPServer("example")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


@server.tool(annotations=READ_ONLY)
def current_time(utc_offset_hours: float = 7) -> str:
    """The date and time now at a UTC offset in hours (7 for Viet Nam, 0 for UTC)."""
    if not -14 <= utc_offset_hours <= 14:
        raise ToolError("the offset must be between -14 and 14 hours")
    zone = timezone(timedelta(hours=utc_offset_hours))
    return datetime.now(zone).strftime("%A %Y-%m-%d %H:%M:%S UTC%z")


@server.tool(annotations=READ_ONLY)
def count_words(text: str) -> dict[str, int]:
    """How many words, lines and characters a text has."""
    return {"words": len(text.split()), "lines": len(text.splitlines()), "characters": len(text)}


if __name__ == "__main__":
    server.run("stdio")
