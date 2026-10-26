"""Script commands: run a file from the scripts folder and post its output."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.bot.scripts import (
    MAX_REPLY_CHARS,
    ScriptError,
    list_scripts,
    resolve_script,
    result_to_reply,
    run_script,
)
from app.bot import streaming
from app.bot.streaming import STREAM_CURSOR
from app.bot.types import MessageContext
from app.config import get_settings
from app.models import BotConfig, MessageLog
from app.rocketchat.ddp import IncomingMessage

CONTEXT = {
    "command": "hello",
    "args": [],
    "args_text": "",
    "groups": {"city": "Hanoi"},
    "user": {"id": "U1", "username": "bo", "mention": "@bo"},
    "room": {"id": "R1", "type": "d"},
    "message": {"id": "M1", "text": "/hello"},
    "now": "2026-10-20T09:00:00+00:00",
}


@pytest.fixture
def scripts(tmp_path, monkeypatch) -> Path:
    """A scripts folder of our own, wired into the settings."""
    root = tmp_path / "scripts"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "scripts_dir", str(root))
    return root


def write(root: Path, name: str, body: str, *, executable: bool = False) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


async def run(root: Path, name: str, *args: str, timeout: float = 10, context=None):
    return await run_script(name, args=list(args), context=context or CONTEXT, timeout=timeout, root=root)


# --------------------------------------------------------------------------- #
# Finding scripts
# --------------------------------------------------------------------------- #


def test_only_runnable_visible_files_are_listed(scripts):
    write(scripts, "hello.py", "print('hi')")
    write(scripts, "disk.sh", "df -h")
    write(scripts, "tool", "#!/bin/sh\necho ok", executable=True)
    write(scripts, "deploy/status.sh", "echo up")
    write(scripts, "notes.txt", "not code")
    write(scripts, "README.md", "# docs")
    write(scripts, "_lib/helper.py", "X = 1")
    write(scripts, ".secret.sh", "echo no")

    listed = {(s.name, s.runner) for s in list_scripts(scripts)}
    assert listed == {
        ("deploy/status.sh", "bash"),
        ("disk.sh", "bash"),
        ("hello.py", "python"),
        ("tool", "executable"),
    }


def test_a_missing_folder_lists_nothing(tmp_path):
    assert list_scripts(tmp_path / "nope") == []


@pytest.mark.parametrize("name", ["../outside.py", "/etc/passwd", "", "missing.py", "_lib/helper.py"])
def test_unsafe_or_unknown_names_are_refused(scripts, name):
    write(scripts, "_lib/helper.py", "X = 1")
    (scripts.parent / "outside.py").write_text("print('escaped')")
    with pytest.raises(ScriptError):
        resolve_script(name, scripts)


def test_a_symlink_out_of_the_folder_is_refused(scripts):
    outside = scripts.parent / "outside.py"
    outside.write_text("print('escaped')")
    (scripts / "link.py").symlink_to(outside)
    with pytest.raises(ScriptError, match="outside"):
        resolve_script("link.py", scripts)
    assert list_scripts(scripts) == []


def test_a_plain_file_without_the_exec_bit_is_refused(scripts):
    write(scripts, "tool", "#!/bin/sh\necho ok")
    with pytest.raises(ScriptError, match="not executable"):
        resolve_script("tool", scripts)


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


async def test_arguments_arrive_verbatim_and_never_reach_a_shell(scripts, tmp_path):
    write(scripts, "echo.py", "import sys\nprint(repr(sys.argv[1:]))")
    marker = tmp_path / "pwned"
    result = await run(scripts, "echo.py", "two words", f"$(touch {marker})", "; ls")
    assert result.exit_code == 0
    assert result.stdout.strip() == repr(["two words", f"$(touch {marker})", "; ls"])
    assert not marker.exists()


async def test_the_message_context_arrives_on_stdin_and_in_the_environment(scripts):
    write(
        scripts,
        "ctx.py",
        "import json, os, sys\n"
        "data = json.load(sys.stdin)\n"
        "print(data['user']['username'], data['groups']['city'])\n"
        "print(os.environ['BOT_USERNAME'], os.environ['BOT_ROOM_ID'], os.environ['BOT_GROUP_CITY'])\n",
    )
    result = await run(scripts, "ctx.py")
    assert result.stdout.splitlines() == ["bo Hanoi", "bo R1 Hanoi"]


async def test_the_bots_secrets_are_not_passed_on(scripts, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "do-not-leak")
    monkeypatch.setenv("WEATHER_KEY", "shared-on-purpose")
    monkeypatch.setattr(get_settings(), "script_env_passthrough", "WEATHER_KEY")
    write(
        scripts,
        "env.py",
        "import os\nprint(os.environ.get('APP_SECRET_KEY'), os.environ.get('WEATHER_KEY'))",
    )
    result = await run(scripts, "env.py")
    assert result.stdout.strip() == "None shared-on-purpose"


async def test_scripts_run_in_their_own_folder(scripts):
    write(scripts, "sub/where.sh", "pwd")
    result = await run(scripts, "sub/where.sh")
    assert Path(result.stdout.strip()).resolve() == (scripts / "sub").resolve()


async def test_a_shebang_script_runs_directly(scripts):
    write(scripts, "tool", "#!/bin/sh\necho \"from sh: $1\"", executable=True)
    assert (await run(scripts, "tool", "x")).stdout.strip() == "from sh: x"


async def test_a_slow_script_and_its_children_are_killed(scripts, tmp_path):
    pid_file = tmp_path / "child.pid"
    write(scripts, "slow.sh", f"sleep 30 &\necho $! > {pid_file}\nwait")
    started = time.monotonic()
    result = await run(scripts, "slow.sh", timeout=0.5)

    assert result.timed_out and time.monotonic() - started < 5
    child = int(pid_file.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    with pytest.raises(ScriptError, match="longer than 0.5s"):
        result_to_reply("slow.sh", result, 0.5)


async def test_a_failure_reports_the_exit_code_and_last_stderr_line(scripts):
    write(scripts, "fail.sh", "echo working\necho 'step 1' >&2\necho 'no such host' >&2\nexit 3")
    result = await run(scripts, "fail.sh")
    with pytest.raises(ScriptError) as caught:
        result_to_reply("fail.sh", result, 30)
    assert str(caught.value) == "`fail.sh` failed (exit code 3): no such host"


async def test_huge_output_is_truncated(scripts):
    write(scripts, "loud.py", "print('x' * 200_000)")
    reply = result_to_reply("loud.py", await run(scripts, "loud.py"), 30)
    assert len(reply) < MAX_REPLY_CHARS + 50
    assert reply.endswith("(output truncated)")


async def test_output_is_reported_as_it_is_printed(scripts):
    # A plain print, no flush=True: the bot runs Python unbuffered.
    write(scripts, "slow.py", "import time\nprint('one')\ntime.sleep(0.3)\nprint('two')")
    seen: list[str] = []
    result = await run_script(
        "slow.py", args=[], context=CONTEXT, timeout=10, root=scripts, on_output=seen.append
    )
    assert seen == ["one\n", "one\ntwo\n"]
    assert result.stdout == "one\ntwo\n"


async def test_a_character_split_between_writes_arrives_whole(scripts):
    write(
        scripts,
        "split.py",
        "import sys, time\n"
        "sys.stdout.buffer.write(b'Vi\\xe1'); sys.stdout.flush(); time.sleep(0.2)\n"
        "sys.stdout.buffer.write(b'\\xbb\\x87t')",
    )
    seen: list[str] = []
    await run_script("split.py", args=[], context=CONTEXT, timeout=10, root=scripts,
                     on_output=seen.append)
    assert seen[-1] == "Việt"
    assert not any("\ufffd" in text for text in seen)


async def test_no_output_is_confirmed(scripts):
    write(scripts, "quiet.sh", "true")
    assert result_to_reply("quiet.sh", await run(scripts, "quiet.sh"), 30) == ":white_check_mark: Done."


# --------------------------------------------------------------------------- #
# As a command
# --------------------------------------------------------------------------- #


SCRIPT_COMMAND = {
    "name": "hello",
    "trigger_type": "keyword",
    "match_mode": "prefix",
    "pattern": "hello",
    "response_type": "script",
    "script_name": "hello.py",
}


async def test_a_script_command_answers_in_chat(scripts, admin_client, app_env, gateway):
    write(
        scripts,
        "hello.py",
        "import os, sys\nprint(f\"Hi @{os.environ['BOT_USERNAME']}: {sys.argv[1:]}\")",
    )
    created = await admin_client.post("/api/commands", json=SCRIPT_COMMAND)
    assert created.status_code == 201
    assert created.json()["script_timeout_seconds"] == 30

    await app_env.bot._handle(
        IncomingMessage(
            message_id="M1",
            room_id="R1",
            text='hello one "two words"',  # no slash: Rocket.Chat would eat it
            sender_id="U1",
            sender_username="bo",
            room_type="d",
        )
    )
    assert gateway.last_text == "Hi @bo: ['one', 'two words']"


async def test_a_failing_script_is_logged_as_an_error(scripts, admin_client, app_env, gateway):
    write(scripts, "hello.py", "import sys\nsys.exit('boom')")
    await admin_client.post("/api/commands", json=SCRIPT_COMMAND)
    await app_env.bot._handle(
        IncomingMessage(message_id="M1", room_id="R1", text="hello", sender_id="U1",
                        sender_username="bo", room_type="d")
    )

    assert gateway.last_text == ":warning: `hello.py` failed (exit code 1): boom"
    async with app_env.session_factory() as session:
        log = (await session.scalars(select(MessageLog))).one()
    assert log.status == "error" and log.matched_command == "hello"


async def test_a_script_mentioned_in_a_channel_gets_arguments(
    scripts, admin_client, app_env, gateway
):
    write(scripts, "hello.py", "import sys\nprint(sys.argv[1:])")
    await admin_client.post("/api/commands", json=SCRIPT_COMMAND)
    message = IncomingMessage(message_id="M1", room_id="C1", text="@taskbot hello /var -h",
                              sender_id="U1", sender_username="bo", room_type="c")
    reply = await app_env.bot.responder.respond(
        MessageContext.from_incoming(message, bot_username="taskbot")
    )
    assert reply.text == "['/var', '-h']"


async def test_a_script_keyword_must_take_arguments(scripts, admin_client):
    write(scripts, "hello.py", "print('hi')")
    exact = await admin_client.post(
        "/api/commands", json={**SCRIPT_COMMAND, "match_mode": "exact"}
    )
    assert exact.status_code == 422 and "prefix" in exact.json()["detail"]

    created = (await admin_client.post("/api/commands", json=SCRIPT_COMMAND)).json()
    switched = await admin_client.put(
        f"/api/commands/{created['id']}", json={"match_mode": "contains"}
    )
    assert switched.status_code == 422


async def test_slash_triggers_are_refused(admin_client):
    created = await admin_client.post(
        "/api/commands",
        json={"name": "x", "trigger_type": "slash", "pattern": "x", "static_response": "y"},
    )
    assert created.status_code == 422


async def test_a_command_must_point_at_an_existing_script(scripts, admin_client):
    response = await admin_client.post("/api/commands", json=SCRIPT_COMMAND)
    assert response.status_code == 422 and "does not exist" in response.json()["detail"]

    no_name = await admin_client.post("/api/commands", json={**SCRIPT_COMMAND, "script_name": None})
    assert no_name.status_code == 422


async def test_the_timeout_is_bounded(scripts, admin_client):
    write(scripts, "hello.py", "print(1)")
    response = await admin_client.post(
        "/api/commands", json={**SCRIPT_COMMAND, "script_timeout_seconds": 3600}
    )
    assert response.status_code == 422


async def test_the_portal_lists_the_scripts_folder(scripts, admin_client):
    write(scripts, "hello.py", "print(1)")
    body = (await admin_client.get("/api/commands/scripts")).json()
    assert body["directory"] == str(scripts.resolve())
    assert body["scripts"] == [{"name": "hello.py", "runner": "python"}]


async def test_a_dry_run_names_the_script_without_running_it(scripts, admin_client, tmp_path):
    marker = tmp_path / "ran"
    write(scripts, "hello.py", f"open({str(marker)!r}, 'w').close()")
    await admin_client.post("/api/commands", json=SCRIPT_COMMAND)

    result = (
        await admin_client.post("/api/commands/test", json={"text": "/hello", "dry_run": True})
    ).json()
    assert result["reply"] == "(dry run) would dispatch script `hello.py`"
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# Streaming mode: output appears in chat while the script runs
# --------------------------------------------------------------------------- #


@pytest.fixture
async def live_mode(app_env, gateway, monkeypatch):
    """Streaming replies on, and every message the bot posts recorded."""
    monkeypatch.setattr(streaming, "LIVE_MIN_INTERVAL_SECONDS", 0.05)
    async with app_env.session_factory() as session:
        config = await session.get(BotConfig, 1)
        config.reply_mode = "stream"
        config.stream_interval_ms = 50
        config.config_version += 1
        await session.commit()
    app_env.config_store.invalidate()

    posted: list[str] = []
    send = gateway.send_message

    async def record(room_id, text, **kwargs):
        posted.append(text)
        return await send(room_id, text, **kwargs)

    monkeypatch.setattr(gateway, "send_message", record)
    return posted


def hello(text: str = "hello") -> IncomingMessage:
    return IncomingMessage(message_id="M1", room_id="R1", text=text, sender_id="U1",
                           sender_username="bo", room_type="d")


async def test_a_scripts_prints_appear_in_chat_while_it_runs(
    scripts, admin_client, app_env, gateway, live_mode
):
    write(scripts, "hello.py", "import time\nprint('step 1')\ntime.sleep(0.3)\nprint('step 2')")
    await admin_client.post("/api/commands", json=SCRIPT_COMMAND)
    await app_env.bot._handle(hello())

    # Posted once, with only the first line, then edited until complete.
    assert live_mode == ["step 1" + STREAM_CURSOR]
    assert len(gateway.sent) == 1 and gateway.last_text == "step 1\nstep 2"
    async with app_env.session_factory() as session:
        log = (await session.scalars(select(MessageLog))).one()
    assert log.status == "ok" and log.text_out == "step 1\nstep 2"


async def test_a_failure_after_some_output_keeps_the_output(
    scripts, admin_client, app_env, gateway, live_mode
):
    write(scripts, "hello.py", "import sys, time\nprint('step 1')\ntime.sleep(0.2)\nsys.exit('boom')")
    await admin_client.post("/api/commands", json=SCRIPT_COMMAND)
    await app_env.bot._handle(hello())

    assert len(gateway.sent) == 1
    assert gateway.last_text == "step 1\n\n:warning: `hello.py` failed (exit code 1): boom"
    async with app_env.session_factory() as session:
        log = (await session.scalars(select(MessageLog))).one()
    assert log.status == "error"


async def test_a_silent_script_still_confirms_in_streaming_mode(
    scripts, admin_client, app_env, gateway, live_mode
):
    write(scripts, "hello.py", "pass")
    await admin_client.post("/api/commands", json=SCRIPT_COMMAND)
    await app_env.bot._handle(hello())
    assert live_mode == [":white_check_mark: Done."] and gateway.updates == []


async def test_testing_a_script_in_the_portal_posts_nothing_in_streaming_mode(
    scripts, admin_client, gateway, live_mode
):
    write(scripts, "hello.py", "import time\nprint('step 1')\ntime.sleep(0.2)\nprint('step 2')")
    await admin_client.post("/api/commands", json=SCRIPT_COMMAND)

    result = (
        await admin_client.post("/api/commands/test", json={"text": "/hello", "dry_run": False})
    ).json()
    assert result["reply"] == "step 1\nstep 2"
    assert live_mode == [] and gateway.updates == []
