"""Runs the scripts in the scripts folder for `script` commands.

Who can run code on the server is decided by who can write to the scripts
folder: the portal can bind a command to a file that is already there, but it
cannot create or change one.

A script gets the command's arguments as argv (never through a shell), the
message context as JSON on stdin and as BOT_* environment variables, and its
stdout becomes the reply. A caller can also watch stdout as it is printed
(`on_output`), which is how streaming mode shows a script's progress live.
Its environment is built from scratch, so the bot's own secrets
(APP_SECRET_KEY, the Rocket.Chat token) never reach it.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings

# Interpreters by extension. Anything else must be executable (shebang).
INTERPRETERS: dict[str, list[str]] = {
    ".py": [sys.executable],
    ".sh": ["bash"],
    ".js": ["node"],
}
RUNNER_LABELS = {".py": "python", ".sh": "bash", ".js": "node"}

MAX_OUTPUT_BYTES = 64 * 1024
# Rocket.Chat refuses messages over 5000 characters by default.
MAX_REPLY_CHARS = 4000
MAX_LISTED = 500
_concurrency: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None

_ENV_NAME_RE = re.compile(r"[^A-Za-z0-9_]")

# Called with all of stdout so far, each time the script prints more.
OutputCallback = Callable[[str], None]


class ScriptError(RuntimeError):
    """The script could not be found, failed, or ran out of time."""


@dataclass(slots=True, frozen=True)
class ScriptInfo:
    name: str  # relative to the scripts folder, with "/" separators
    runner: str


@dataclass(slots=True)
class ScriptResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False


def _slots() -> asyncio.Semaphore:
    """Caps scripts running at once (APP_SCRIPT_MAX_CONCURRENCY)."""
    global _concurrency
    loop = asyncio.get_running_loop()
    # A semaphore belongs to one event loop; tests start a fresh loop each.
    if _concurrency is None or _concurrency[0] is not loop:
        _concurrency = (loop, asyncio.Semaphore(get_settings().script_max_concurrency))
    return _concurrency[1]


def scripts_root() -> Path:
    return Path(get_settings().scripts_dir).expanduser().resolve()


def _runner_for(path: Path) -> list[str] | None:
    """The argv prefix that runs `path`, or None if it cannot be run."""
    interpreter = INTERPRETERS.get(path.suffix.lower())
    if interpreter is not None:
        return interpreter if shutil.which(interpreter[0]) else None
    if os.access(path, os.X_OK):
        return []
    return None


def _is_hidden(relative: Path) -> bool:
    return any(part.startswith((".", "_")) for part in relative.parts)


def list_scripts(root: Path | None = None) -> list[ScriptInfo]:
    """Every runnable file under the folder, skipping hidden and _private ones."""
    root = root or scripts_root()
    if not root.is_dir():
        return []
    found: list[ScriptInfo] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _is_hidden(relative) or not path.is_file():
            continue
        try:
            resolved = _resolve(relative.as_posix(), root)
        except ScriptError:
            continue
        if _runner_for(resolved) is None:
            continue
        label = RUNNER_LABELS.get(path.suffix.lower(), "executable")
        found.append(ScriptInfo(relative.as_posix(), label))
        if len(found) >= MAX_LISTED:
            break
    return found


def _resolve(name: str, root: Path) -> Path:
    """Map a script name to a file inside the folder, refusing escapes."""
    relative = Path(name)
    if not name or relative.is_absolute() or ".." in relative.parts:
        raise ScriptError(f"{name!r} is not a valid script name")
    path = (root / relative).resolve()
    # resolve() follows symlinks, so a link pointing outside is caught here too.
    if not path.is_relative_to(root):
        raise ScriptError(f"{name!r} is outside the scripts folder")
    if not path.is_file():
        raise ScriptError(f"script `{name}` does not exist")
    return path


def resolve_script(name: str, root: Path | None = None) -> tuple[Path, list[str]]:
    """The script's path and the argv prefix to run it with."""
    root = root or scripts_root()
    path = _resolve(name, root)
    if _is_hidden(path.relative_to(root)):
        raise ScriptError(f"script `{name}` is hidden")
    runner = _runner_for(path)
    if runner is None:
        interpreter = INTERPRETERS.get(path.suffix.lower())
        if interpreter:
            raise ScriptError(f"`{interpreter[0]}` is not installed, so `{name}` cannot run")
        raise ScriptError(f"script `{name}` is not executable")
    return path, runner


def build_env(context: dict[str, Any]) -> dict[str, str]:
    """A minimal environment plus BOT_* variables describing the message."""
    settings = get_settings()
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        # Python buffers stdout when it is a pipe, so prints would only arrive
        # at exit; unbuffered, they can be shown in chat as they happen.
        "PYTHONUNBUFFERED": "1",
    }
    for name in ("TZ", *settings.script_env_passthrough_list):
        if name in os.environ:
            env[name] = os.environ[name]

    user = context.get("user", {})
    room = context.get("room", {})
    message = context.get("message", {})
    values = {
        "BOT_COMMAND": context.get("command", ""),
        "BOT_ARGS": context.get("args_text", ""),
        "BOT_USER_ID": user.get("id"),
        "BOT_USERNAME": user.get("username"),
        "BOT_ROOM_ID": room.get("id"),
        "BOT_ROOM_TYPE": room.get("type"),
        "BOT_MESSAGE_ID": message.get("id"),
        "BOT_MESSAGE_TEXT": message.get("text"),
    }
    for group, value in (context.get("groups") or {}).items():
        values[f"BOT_GROUP_{_ENV_NAME_RE.sub('_', group).upper()}"] = value
    for key, value in values.items():
        # Environment values cannot hold NUL bytes.
        env[key] = str(value or "").replace("\x00", "")
    return env


async def _read_capped(
    stream: asyncio.StreamReader, limit: int, on_output: OutputCallback | None = None
) -> tuple[bytes, bool]:
    """Read everything, keeping the first `limit` bytes.

    The rest is drained and dropped, so a chatty script cannot exhaust memory
    or block on a full pipe. `on_output` sees the kept text as it arrives.
    """
    kept = bytearray()
    truncated = False
    # Incremental, so a character split across two reads is not mangled.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = ""
    while chunk := await stream.read(65536):
        room = limit - len(kept)
        if room > 0:
            kept += chunk[:room]
            if on_output is not None:
                text += decoder.decode(chunk[:room])
                on_output(text)
        if len(chunk) > room:
            truncated = True
    return bytes(kept), truncated


def _kill(process: asyncio.subprocess.Process) -> None:
    """Kill the script and anything it started."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


async def run_script(
    name: str,
    *,
    args: list[str],
    context: dict[str, Any],
    timeout: float,
    root: Path | None = None,
    on_output: OutputCallback | None = None,
) -> ScriptResult:
    root = root or scripts_root()
    path, runner = resolve_script(name, root)
    timeout = max(0.1, min(timeout, get_settings().script_max_timeout_seconds))
    stdin = json.dumps(context, default=str, ensure_ascii=False).encode()

    async with _slots():
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *runner,
                str(path),
                *args,
                cwd=str(path.parent),
                env=build_env(context),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # its own process group, for _kill
            )
        except OSError as exc:
            raise ScriptError(f"could not start `{name}`: {exc}") from exc

        async def feed() -> None:
            # A script that never reads stdin is fine.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.write(stdin)  # type: ignore[union-attr]
                await process.stdin.drain()  # type: ignore[union-attr]
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.close()  # type: ignore[union-attr]

        timed_out = False
        work = asyncio.gather(
            feed(),
            _read_capped(process.stdout, MAX_OUTPUT_BYTES, on_output),  # type: ignore[arg-type]
            _read_capped(process.stderr, MAX_OUTPUT_BYTES),  # type: ignore[arg-type]
            process.wait(),
        )
        try:
            _, (out, out_cut), (err, _), _ = await asyncio.wait_for(work, timeout)
        except asyncio.TimeoutError:
            timed_out = True
            _kill(process)
            await process.wait()
            out, out_cut, err = b"", False, b""
        except asyncio.CancelledError:
            _kill(process)
            raise

    return ScriptResult(
        exit_code=process.returncode,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        duration_ms=int((time.monotonic() - started) * 1000),
        timed_out=timed_out,
        truncated=out_cut,
    )


def result_to_reply(name: str, result: ScriptResult, timeout: float) -> str:
    """Turn a finished run into chat text, or raise ScriptError for a failure."""
    if result.timed_out:
        raise ScriptError(f"`{name}` took longer than {timeout:g}s and was stopped")
    if result.exit_code != 0:
        lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
        detail = f": {lines[-1][:300]}" if lines else ""
        raise ScriptError(f"`{name}` failed (exit code {result.exit_code}){detail}")

    return clip_output(result.stdout, result.truncated) or ":white_check_mark: Done."


def clip_output(text: str, truncated: bool = False) -> str:
    """Script output cut down to what fits in one chat message."""
    text = text.strip()
    if text and (truncated or len(text) > MAX_REPLY_CHARS):
        text = text[:MAX_REPLY_CHARS].rstrip() + "\n… (output truncated)"
    return text
