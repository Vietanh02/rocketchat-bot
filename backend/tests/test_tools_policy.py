"""The tool policy, MCP servers and skills."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from app.agent.policy import SAFE, TRUSTED, ToolPolicySettings, globs
from app.bot.config_store import ensure_tool_policy
from app.config import get_settings
from app.integrations import mcp as mcp_client
from app.models import BotConfig
from tests.test_agent import model  # noqa: F401
from tests.test_llm import tool_use
from tests.test_responder import context


def call(tool: str, **arguments) -> dict:
    """A canned answer calling `tool` (whose arguments may include `name`)."""
    return {"tool": tool, "arguments": json.dumps(arguments)}


def offered(model, index: int = 0) -> list[str]:
    return [t["function"]["name"] for t in model.requests[index].get("tools", [])]


async def set_policy(runtime, **fields):
    async with runtime.session_factory() as session:
        config = await ensure_tool_policy(session)
        for name, value in fields.items():
            setattr(config, name, value)
        (await session.get(BotConfig, 1)).config_version += 1
        await session.commit()
    runtime.config_store.invalidate()


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def test_globs_allow_and_deny_by_tool_name():
    assert globs("mcp__*\n# comment\n\nskill_run, kb_*") == ("mcp__*", "skill_run", "kb_*")
    policy = ToolPolicySettings(trusted=frozenset({"an"}))
    assert policy.profile("@An") == TRUSTED and policy.profile("bo") == SAFE
    assert not policy.permits(SAFE, "mcp__github__create_issue")
    assert not policy.permits(SAFE, "skill_run") and policy.permits(SAFE, "skill_read")
    assert policy.permits(TRUSTED, "mcp__github__create_issue")
    narrow = ToolPolicySettings(safe_allow=("kb_*",), safe_deny=())
    assert narrow.permits(SAFE, "kb_search") and not narrow.permits(SAFE, "memory_save")


async def test_each_person_gets_the_tools_of_their_profile(app_env, gateway, model):
    await set_policy(app_env, safe_deny="memory_*\nstart_job", trusted_usernames="bo")
    await app_env.bot.responder.respond(context("hello", sender_username="an", sender_id="U2"))
    await app_env.bot.responder.respond(context("hello", message_id="M2"))  # bo
    assert not any(name.startswith("memory_") for name in offered(model, 0))
    assert "start_job" not in offered(model, 0)
    assert "memory_save" in offered(model, 1) and "start_job" in offered(model, 1)


async def test_the_portal_shows_who_gets_which_tool(admin_client):
    body = (await admin_client.put("/api/policy", json={"trusted_usernames": "an\nbo"})).json()
    assert body["trusted_usernames"] == "an\nbo" and "mcp__*" in body["safe_deny"]
    tools = {t["name"]: t for t in (await admin_client.get("/api/policy/tools")).json()}
    assert tools["skill_run"] == {"name": "skill_run", "kind": "skills", "safe": False, "trusted": True}
    assert tools["kb_search"]["safe"] is True


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #


def demo_server(looked_up: list[str] | None = None) -> MCPServer:
    server = MCPServer("demo")

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def boom() -> str:
        """Always fails."""
        raise ValueError("nope")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def lookup(key: str) -> str:
        """Look a key up."""
        if looked_up is not None:
            looked_up.append(key)
        return key.upper()

    return server


class Sessions:
    """Sessions opened to the demo server. `restart()` breaks the open ones,
    as when the server process is restarted."""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.looked_up: list[str] = []
        self.generation = 0

    def restart(self) -> None:
        self.generation += 1


class DemoClient:
    def __init__(self, server: MCPServer, sessions: Sessions) -> None:
        self._inner = Client(server, cache=None)
        self._sessions = sessions
        self._generation = sessions.generation

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)

    def _check(self) -> None:
        if self._generation != self._sessions.generation:
            raise ConnectionError("session terminated")

    async def list_tools(self, cursor=None):
        return await self._inner.list_tools(cursor=cursor)

    async def send_ping(self):
        self._check()
        return await self._inner.send_ping()

    async def call_tool(self, name, arguments):
        self._check()
        return await self._inner.call_tool(name, arguments)


@pytest.fixture
def mcp_demo(monkeypatch):
    """Every MCP server is the in-process demo server; records each session opened."""
    sessions = Sessions()
    server = demo_server(sessions.looked_up)

    def factory(spec):
        sessions.opened.append(spec.name)
        return DemoClient(server, sessions)

    monkeypatch.setattr(mcp_client, "client_factory", factory)
    monkeypatch.setattr(mcp_client, "pool", mcp_client.McpPool())
    return sessions


async def add_server(admin_client, **fields) -> dict:
    body = {"name": "demo", "transport": "http", "url": "http://mcp.test/mcp", "headers": {"Authorization": "Bearer s3cret"}}
    body.update(fields)
    response = await admin_client.post("/api/mcp", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_an_mcp_servers_tools_are_listed_off_and_turned_on_one_by_one(admin_client, mcp_demo):
    server = await add_server(admin_client)
    assert server["header_names"] == ["Authorization"] and "s3cret" not in json.dumps(server)
    refreshed = (await admin_client.post(f"/api/mcp/{server['id']}/refresh")).json()
    tools = {t["name"]: t for t in refreshed["tools"]}
    assert set(tools) == {"add", "boom", "lookup"} and not any(t["enabled"] for t in tools.values())
    # What the server says of each tool is kept.
    assert tools["lookup"]["read_only"] and not tools["lookup"]["destructive"]
    assert tools["boom"]["destructive"] and not tools["add"]["read_only"]
    assert tools["add"]["tool_name"] == "mcp__demo__add"
    assert tools["add"]["input_schema"]["required"] == ["a", "b"]

    on = await admin_client.put(f"/api/mcp/{server['id']}/tools/{tools['add']['id']}", json={"enabled": True})
    assert [t["enabled"] for t in on.json()["tools"] if t["name"] == "add"] == [True]
    tried = (
        await admin_client.post(
            f"/api/mcp/{server['id']}/tools/{tools['add']['id']}/call", json={"arguments": {"a": 2, "b": 3}}
        )
    ).json()
    assert tried["ok"] is True and tried["text"] == "5"
    failed = (await admin_client.post(f"/api/mcp/{server['id']}/tools/{tools['boom']['id']}/call", json={})).json()
    assert failed["ok"] is False and "boom" in failed["text"]


async def test_the_model_calls_an_mcp_tool_for_a_trusted_person(admin_client, app_env, gateway, model, mcp_demo):
    server = await add_server(admin_client)
    tools = {t["name"]: t for t in (await admin_client.post(f"/api/mcp/{server['id']}/refresh")).json()["tools"]}
    await admin_client.put(f"/api/mcp/{server['id']}/tools/{tools['add']['id']}", json={"enabled": True})
    await set_policy(app_env, trusted_usernames="bo")

    model.answers = [tool_use("mcp__demo__add", a=40, b=2), "It is 42."]
    reply = await app_env.bot.responder.respond(context("what is 40 plus 2?"))
    assert reply.text == "It is 42."
    assert "mcp__demo__add" in offered(model) and "mcp__demo__boom" not in offered(model)
    result = model.requests[1]["messages"][-1]
    assert result["role"] == "tool" and json.loads(result["content"]) == {"result": "42"}

    # Someone else, on the safe profile, is not offered it.
    model.answers = ["ok"]
    await app_env.bot.responder.respond(context("hi", sender_username="an", sender_id="U2", message_id="M2"))
    assert "mcp__demo__add" not in offered(model, 2)


async def test_an_unreachable_mcp_server_is_reported(admin_client, monkeypatch):
    def broken(spec):
        raise OSError("connection refused")

    monkeypatch.setattr(mcp_client, "client_factory", broken)
    server = await add_server(admin_client)
    response = await admin_client.post(f"/api/mcp/{server['id']}/refresh")
    assert response.status_code == 502 and "connection refused" in response.json()["detail"]
    listed = (await admin_client.get("/api/mcp")).json()[0]
    assert "connection refused" in listed["last_error"]


async def test_mcp_servers_need_what_their_transport_needs(admin_client):
    response = await admin_client.post("/api/mcp", json={"name": "x", "transport": "stdio"})
    assert response.status_code == 422
    response = await admin_client.post("/api/mcp", json={"name": "bad name", "url": "http://a/b"})
    assert response.status_code == 422


async def tool_ids(admin_client, server: dict) -> dict[str, int]:
    refreshed = (await admin_client.post(f"/api/mcp/{server['id']}/refresh")).json()
    return {t["name"]: t["id"] for t in refreshed["tools"]}


async def try_tool(admin_client, server: dict, tool_id: int, **arguments) -> dict:
    response = await admin_client.post(
        f"/api/mcp/{server['id']}/tools/{tool_id}/call", json={"arguments": arguments}
    )
    return response.json()


async def test_a_servers_session_is_kept_open_between_calls(admin_client, mcp_demo):
    server = await add_server(admin_client)
    ids = await tool_ids(admin_client, server)
    assert mcp_demo.opened == ["demo"]  # the refresh, on a session of its own

    for a in range(3):
        assert (await try_tool(admin_client, server, ids["add"], a=a, b=1))["text"] == str(a + 1)
    assert len(mcp_demo.opened) == 2  # one more, kept for all three calls
    assert (await admin_client.get("/api/mcp")).json()[0]["connected"] is True

    # New settings close it; the next call opens another.
    await admin_client.put(f"/api/mcp/{server['id']}", json={"timeout_seconds": 20})
    await try_tool(admin_client, server, ids["add"], a=1, b=1)
    assert len(mcp_demo.opened) == 3

    # Kept for 0 seconds: a session per call.
    await admin_client.put(f"/api/mcp/{server['id']}", json={"keep_alive_seconds": 0})
    await try_tool(admin_client, server, ids["add"], a=1, b=1)
    await try_tool(admin_client, server, ids["add"], a=1, b=1)
    assert len(mcp_demo.opened) == 5
    await mcp_client.pool.close_all()


async def test_a_broken_session_is_replaced_and_only_read_only_tools_are_retried(admin_client, mcp_demo):
    server = await add_server(admin_client)
    ids = await tool_ids(admin_client, server)
    assert (await try_tool(admin_client, server, ids["lookup"], key="a"))["text"] == "A"
    opened = len(mcp_demo.opened)

    # The server restarted: a read-only tool is called again on a new session.
    mcp_demo.restart()
    assert (await try_tool(admin_client, server, ids["lookup"], key="b"))["text"] == "B"
    assert len(mcp_demo.opened) == opened + 1

    # Any other tool is not: the call may have done its work. The next one works.
    mcp_demo.restart()
    failed = await try_tool(admin_client, server, ids["add"], a=1, b=2)
    assert failed["ok"] is False and "session terminated" in failed["text"]
    assert (await try_tool(admin_client, server, ids["add"], a=1, b=2))["text"] == "3"
    await mcp_client.pool.close_all()


async def test_an_mcp_tool_opened_to_everyone_reuses_its_answers(admin_client, app_env, gateway, model, mcp_demo):
    server = await add_server(admin_client)
    ids = await tool_ids(admin_client, server)
    url = f"/api/mcp/{server['id']}/tools/{ids['lookup']}"
    body = (await admin_client.put(url, json={"enabled": True, "everyone": True, "cache_seconds": 60})).json()
    lookup = next(t for t in body["tools"] if t["name"] == "lookup")
    assert lookup["enabled"] and lookup["everyone"] and lookup["cache_seconds"] == 60
    await admin_client.put(f"/api/mcp/{server['id']}/tools/{ids['add']}", json={"enabled": True})

    # Someone on the safe profile gets it, though `mcp__*` is denied to them...
    safe = {"sender_username": "an", "sender_id": "U2"}
    model.answers = [tool_use("mcp__demo__lookup", key="x"), "X.", tool_use("mcp__demo__lookup", key="x"), "X again."]
    assert (await app_env.bot.responder.respond(context("look x up", **safe))).text == "X."
    assert "mcp__demo__lookup" in offered(model) and "mcp__demo__add" not in offered(model)
    # ...and the second time the answer is reused.
    await app_env.bot.responder.respond(context("again", message_id="M2", **safe))
    assert mcp_demo.looked_up == ["x"]
    assert json.loads(model.requests[3]["messages"][-1]["content"]) == {"result": "X"}

    tools = {t["name"]: t for t in (await admin_client.get("/api/policy/tools")).json()}
    assert tools["mcp__demo__lookup"]["safe"] is True and tools["mcp__demo__add"]["safe"] is False
    await mcp_client.pool.close_all()


PID_SERVER = """
import os
from mcp.server.mcpserver import MCPServer

server = MCPServer("pid")


@server.tool()
def pid() -> int:
    \"\"\"The process id of this server.\"\"\"
    return os.getpid()


if __name__ == "__main__":
    server.run("stdio")
"""


async def test_a_python_server_over_stdio_keeps_its_process(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "mcp_servers_dir", str(tmp_path))
    monkeypatch.setattr(mcp_client, "pool", mcp_client.McpPool())
    (tmp_path / "pid").mkdir()
    (tmp_path / "pid" / "server.py").write_text(PID_SERVER, encoding="utf-8")
    # `python` is the bot's interpreter, run in the servers folder.
    spec = mcp_client.McpServerSpec(
        1, "pid", "stdio", command="python", args=("pid/server.py",), timeout_seconds=30, keep_alive_seconds=60
    )
    assert [t.name for t in await mcp_client.list_tools(spec)] == ["pid"]
    first = await mcp_client.call_tool(spec, "pid", {})
    assert first[0] and first == await mcp_client.call_tool(spec, "pid", {})

    mcp_client.pool.forget(1)
    third = await mcp_client.call_tool(spec, "pid", {})
    assert third[0] and third != first
    await mcp_client.pool.close_all()
    assert len(mcp_client.pool) == 0


async def test_the_example_python_server_offers_read_only_tools():
    spec = mcp_client.McpServerSpec(1, "example", "stdio", command="python", args=("_example/server.py",))
    tools = {t.name: t for t in await mcp_client.list_tools(spec)}
    assert set(tools) == {"current_time", "count_words"} and all(t.read_only for t in tools.values())
    ok, text = await mcp_client.call_tool(spec, "current_time", {"utc_offset_hours": 99})
    assert not ok and "between -14 and 14" in text


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #


def write_skill(root: Path, name: str, front: str, body: str = "Do it well.", scripts: dict | None = None):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")
    for script, text in (scripts or {}).items():
        path = folder / "scripts" / script
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


@pytest.fixture
def skills_dir(tmp_path, monkeypatch, app_env):
    monkeypatch.setattr(get_settings(), "skills_dir", str(tmp_path))
    app_env.bot.responder.agent.skills.invalidate()
    write_skill(
        tmp_path,
        "greet",
        "name: greet\ndescription: Greet people formally.",
        "Run scripts/hello.py with the name.",
        {"hello.py": "import sys\nprint('Good day, ' + ' '.join(sys.argv[1:]))\n"},
    )
    write_skill(tmp_path, "needs-tool", "description: Needs a missing program.\nrequires:\n  bins: [no-such-binary-x]")
    write_skill(tmp_path, "_hidden", "description: Never offered.")
    return tmp_path


async def test_skills_are_listed_to_the_model_and_read_on_demand(app_env, gateway, model, skills_dir):
    model.answers = [call("skill_read", name="greet"), "Good day!"]
    await app_env.bot.responder.respond(context("greet An formally"))
    tools = {t["function"]["name"]: t["function"] for t in model.requests[0]["tools"]}
    assert "- greet: Greet people formally." in tools["skill_read"]["description"]
    assert "needs-tool" not in tools["skill_read"]["description"] and "_hidden" not in json.dumps(tools)
    assert "skill_run" not in tools  # the safe profile
    read = json.loads(model.requests[1]["messages"][-1]["content"])
    assert "Run scripts/hello.py" in read["text"] and read["scripts"] == ["hello.py"]


async def test_a_trusted_person_runs_a_skills_script(app_env, gateway, model, skills_dir):
    await set_policy(app_env, trusted_usernames="bo")
    model.answers = [call("skill_run", name="greet", script="hello.py", args=["An"]), "Done."]
    await app_env.bot.responder.respond(context("greet An formally"))
    ran = json.loads(model.requests[1]["messages"][-1]["content"])
    assert ran["exit_code"] == 0 and ran["stdout"].strip() == "Good day, An"


async def test_skill_files_cannot_leave_their_folder(app_env, gateway, model, skills_dir):
    (skills_dir / "secret.txt").write_text("top secret")
    model.answers = [call("skill_read", name="greet", file="../secret.txt"), "ok"]
    await app_env.bot.responder.respond(context("read it"))
    assert "error" in json.loads(model.requests[1]["messages"][-1]["content"])


async def test_admins_see_and_turn_off_skills(admin_client, app_env, gateway, model, skills_dir):
    skills = {s["name"]: s for s in (await admin_client.get("/api/skills")).json()}
    assert set(skills) == {"greet", "needs-tool"}
    assert skills["greet"]["available"] and not skills["needs-tool"]["available"]
    assert "`no-such-binary-x` is not installed" in skills["needs-tool"]["missing"]
    detail = (await admin_client.get("/api/skills/greet")).json()
    assert "Run scripts/hello.py" in detail["text"]
    off = (await admin_client.put("/api/skills/greet", json={"enabled": False})).json()
    assert off["enabled"] is False and off["available"] is False
    await app_env.bot.responder.respond(context("hello"))
    assert "skill_read" not in offered(model)
