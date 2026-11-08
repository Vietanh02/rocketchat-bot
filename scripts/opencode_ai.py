"""Chat with opencode from Rocket.Chat.

Talks to a headless opencode server (`opencode serve`) over HTTP, so it works
whether the bot runs on the host or in Docker. Each Rocket.Chat user gets their
own opencode session per room, so follow-up messages keep the conversation.

Bind it in the portal: Commands → New command → Response type "Run a script"
→ opencode_ai.py, keyword `ai`, timeout 300. Then:

    ai explain what this repo does
    ai new            start a fresh session (forget the conversation)

Environment (list the names in APP_SCRIPT_ENV_PASSTHROUGH):
    OPENCODE_URL              server address, default http://127.0.0.1:4096
    OPENCODE_SERVER_PASSWORD  basic-auth password the server was started with
    OPENCODE_DIR              project directory opencode works in (optional)
    OPENCODE_MODEL            provider/model, e.g. google/gemma-4-31b-it (optional)
"""

import json
import os
import sys

import httpx

URL = os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096").rstrip("/")
PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
DIRECTORY = os.environ.get("OPENCODE_DIR", "")
MODEL = os.environ.get("OPENCODE_MODEL", "")

context = json.load(sys.stdin)
prompt = context["args_text"].strip()
title = f"rocketchat {context['room']['id']} {context['user']['username']}"

if not prompt:
    print(f"Usage: `{context['command']} <question>` · `{context['command']} new` to start over")
    sys.exit(0)

client = httpx.Client(
    base_url=URL,
    auth=("opencode", PASSWORD) if PASSWORD else None,
    params={"directory": DIRECTORY} if DIRECTORY else None,
    timeout=httpx.Timeout(10, read=None),  # the bot's script timeout bounds the wait
)


def new_session() -> str:
    resp = client.post("/session", json={"title": title})
    resp.raise_for_status()
    return resp.json()["id"]


def current_session() -> str:
    resp = client.get("/session")
    resp.raise_for_status()
    mine = [s for s in resp.json() if s.get("title") == title]
    if not mine:
        return new_session()
    return max(mine, key=lambda s: s["time"]["updated"])["id"]


try:
    if prompt.lower() in ("new", "reset"):
        new_session()
        print("🆕 Started a new opencode session.")
        sys.exit(0)

    body: dict = {"parts": [{"type": "text", "text": prompt}]}
    if MODEL:
        provider, _, model = MODEL.partition("/")
        body["model"] = {"providerID": provider, "modelID": model}

    resp = client.post(f"/session/{current_session()}/message", json=body)
    resp.raise_for_status()
    reply = resp.json()
except httpx.HTTPError as exc:
    print(f"Cannot reach opencode at {URL}: {exc}", file=sys.stderr)
    sys.exit(1)

error = reply.get("info", {}).get("error")
if error:
    message = error.get("data", {}).get("message") or error.get("name", "unknown error")
    print(f"opencode error: {message}", file=sys.stderr)
    sys.exit(1)

parts = reply.get("parts", [])
tools = [p["tool"] for p in parts if p.get("type") == "tool"]
text = "\n\n".join(p["text"] for p in parts if p.get("type") == "text" and p.get("text"))

if tools:
    print(f"_🔧 {', '.join(dict.fromkeys(tools))}_\n")
print(text or "(no text reply)")
