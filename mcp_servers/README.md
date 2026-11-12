# MCP servers started over stdio

The bot starts an MCP server of the **stdio** transport as a process in the
backend container and speaks to it over its stdin/stdout. Such a process runs
in this folder, which is mounted read-only at `/app/mcp_servers`.

## A server in Python

Keep it in a folder of its own here, for example `mcp_servers/tickets/server.py`,
then add it on the portal's **MCP** page:

| Field     | Value                |
|-----------|----------------------|
| Transport | stdio                |
| Command   | `python`             |
| Arguments | `tickets/server.py`  |

`python` is the bot's own interpreter. A server can import everything the bot
has: the `mcp` SDK, `httpx`, `pydantic` and the rest of
`backend/requirements.txt`. A server that needs another package gets it by
adding it to that file and rebuilding the backend image; nothing is installed
while the bot runs.

Secrets (API tokens and the like) go in the server's **environment variables**
on the portal. They are stored encrypted. The proxy settings
(`HTTP_PROXY` and the others) are passed on too.

`_example/server.py` is a working example with two read-only tools. Copy it to
start your own.

## Writing the tools

- Give every tool a docstring. The model reads it to decide when to call the tool.
- Raise `ToolError` (from `mcp.server.mcpserver.exceptions`) for a failure the
  model should read. Any other exception reaches it only as
  "Error executing tool …".
- Mark a tool that changes nothing with
  `annotations=ToolAnnotations(read_only_hint=True)`. On the portal it shows as
  read-only. A read-only tool can be opened to everyone and its answers reused
  for a while. When a kept-open session failed, the bot calls it again on a new
  session.
- Print nothing to stdout: that is the protocol's channel. Log to stderr.

## Sessions are kept open

After the first call, the server's process stays up for the next calls. On this
machine that cut a call from about 0.7 s to a few milliseconds. It is closed
after **Keep alive** seconds idle (300 by default; 0 starts a process for every
call), and when the server's settings change. Edits to the server's code
therefore apply once the process is closed: wait for it to go idle, or save the
server on the portal.
