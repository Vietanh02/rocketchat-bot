# Rocket.Chat chatbot + configuration portal

A chatbot for Rocket.Chat, plus a web portal to configure it without touching code.

- **Chat** — the bot answers direct messages, and answers in channels when mentioned
  (`@taskbot help`).
- **Portal** — commands, API integrations, reminders, bot settings and logs are all editable
  in the browser. Changes take effect on the next message; no restart.
- **API integrations** — describe an HTTP call once (URL, headers, body, how to render the
  reply) and bind it to a command. `weather Hanoi` becomes a real API call.
- **Reminders** — users set their own from chat (`remind daily 09:00 stand-up`), and admins
  manage all of them from the portal.

## Architecture

```
┌─────────────┐   DDP websocket (inbound messages)   ┌──────────────┐
│ Rocket.Chat │ ───────────────────────────────────► │              │
│   :3000     │ ◄─────────────────────────────────── │   backend    │
└─────────────┘   REST (everything outbound)         │  (FastAPI)   │
                                                     │              │
┌─────────────┐          /api  (same origin)         │  ┌────────┐  │
│   portal    │ ───────────────────────────────────► │  │ SQLite │  │
│ (React+nginx)│                                     │  └────────┘  │
└─────────────┘                                      └──────┬───────┘
                                                            │ HTTPS
                                                     ┌──────▼───────┐
                                                     │ your APIs    │
                                                     └──────────────┘
```

Inbound messages arrive on Rocket.Chat's realtime (DDP) stream; **everything the bot does
goes out over REST**. Rocket.Chat is deprecating realtime API methods (removal in 9.0.0) and
steers bots towards Apps-Engine, so the exposure is deliberately confined to one listener
module — `app/rocketchat/ddp.py` — behind the `Gateway` interface in
`app/rocketchat/gateway.py`.

```
backend/app/
├── main.py            FastAPI app; the lifespan starts the bot and scheduler
├── config.py          settings (APP_* environment variables)
├── models.py          SQLAlchemy tables
├── rocketchat/        ddp.py (listen) · rest.py (act) · gateway.py (the seam)
├── bot/               runner · router · responder · access · streaming · scripts · builtins · remind_parser
├── integrations/      executor (HTTP + SSRF guard) · templating (sandboxed Jinja)
├── scheduler/         APScheduler service · trigger/timezone maths
└── api/               auth · config · commands · integrations · tasks · access · logs
scripts/               files that script commands run (see scripts/README.md)
portal/src/
├── api/               typed client for the backend
└── pages/             Dashboard · Commands · Integrations · Reminders · Access · Logs · Settings
```

## Quick start (Docker)

```bash
# 1. Rocket.Chat first — it owns the network the bot joins.
cd rocketchat_server && docker compose up -d

# 2. One-time, if this is a brand-new Mongo: initiate the replica set.
#    Rocket.Chat will crash-loop until you do.
docker compose exec -T mongodb mongosh --quiet --eval \
  'rs.initiate({_id:"rs0",members:[{_id:0,host:"mongodb:27017"}]})'

# 3. Create the bot's Rocket.Chat account (admin → Users → New user, role `bot`).
#    Then, in that account, My Account → Personal Access Tokens → generate one.

# 4. Start the bot and portal.
cd .. && cp backend/.env.example backend/.env   # set APP_SECRET_KEY!
docker compose up -d --build
```

The portal is then at <http://localhost:8081> (`PORTAL_PORT` changes it). For a full
deployment guide (in Vietnamese: requirements, configuration order, backups, upgrades,
release packages, troubleshooting) see [DEPLOY.md](DEPLOY.md). Log in with
`APP_BOOTSTRAP_ADMIN_EMAIL` / `APP_BOOTSTRAP_ADMIN_PASSWORD`, open **Bot settings**, paste the
bot's user ID and access token, press **Test connection**, then **Save & reconnect**. The
sidebar dot turns green when the bot is listening.

Send the bot a DM saying `help` to confirm.

> **If your Docker build cannot resolve DNS** (`Temporary failure in name resolution` while
> pip or npm install), the build container has no working resolver. Build the images with the
> host network instead, then start without rebuilding:
>
> ```bash
> docker build --network=host -t rocketchat_bot-backend ./backend
> docker build --network=host -t rocketchat_bot-portal  ./portal
> docker compose up -d --no-build
> ```

### Proxy, certificates and debugging

Stack-wide settings go in a `.env` next to `docker-compose.yml` (copy `.env.example`):

- `HTTP_PROXY` / `HTTPS_PROXY`: the outgoing proxy, used by the backend, by `embed` (model
  download) and while building the images. `HTTPS_PROXY` defaults to `HTTP_PROXY`.
- `NO_PROXY`: hosts reached directly. The containers' own names are always on the list;
  this adds to it (a Rocket.Chat on the LAN, say).
- `APP_TLS_VERIFY=false`: do not check HTTPS/WSS certificates, for a proxy that re-signs
  traffic. A warning is logged at startup.
- `APP_DEBUG=true` (or `APP_DEBUG=true docker compose up -d backend` for one run): DEBUG
  logs, a traceback with every warning and error, the cause of a failed call in the chat
  reply, and tracebacks in portal API errors.

`docker compose exec backend python -m app.doctor` checks every connection (database,
Rocket.Chat REST and realtime, LLM gateway, embedding, reranker, Qdrant, MCP servers,
integration addresses). Each line says whether it goes direct or via the proxy, and for a
failure, the whole chain of causes. Add `--chat` to also ask the model one question.

## Local development

```bash
# backend
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # set APP_SECRET_KEY, and APP_RC_* if not using the portal
.venv/bin/uvicorn app.main:app --reload --port 8000

# portal (in another shell)
cd portal && npm install && npm run dev   # http://localhost:5173, proxies /api to :8000
```

Tests:

```bash
cd backend && .venv/bin/python -m pytest -q      # 565 tests, no network or Rocket.Chat needed
```

## Configuring the bot

### Commands

A command is a **trigger** plus a **response**.

| Trigger | Matches | Arguments |
|---|---|---|
| `keyword` | the message `exact`ly equals, `prefix`-starts with, or `contains` the pattern | the words after the pattern (`prefix`) or whole message (`contains`) |
| `regex` | the regex matches anywhere | named groups become `{{ groups.name }}` and bind to integration parameters |

There are no slash triggers: Rocket.Chat treats any message starting with `/` as one of its
own slash commands and never delivers it to the bot. Use a `prefix` keyword instead —
`weather Hanoi` in a direct message, `@taskbot weather Hanoi` in a channel. A leading `/`
is ignored where Rocket.Chat does let one through.

Lower `priority` wins when several commands match. Responses are:

- **static** — a Jinja template;
- **integration** — call a configured API and post the rendered result;
- **builtin** — behaviour shipped with the bot (`help`, `status`, `remind`, `tasks`). Built-ins
  are ordinary rows, so you can rename their trigger, hide them from help, or disable them.
- **script** — run a file from the `scripts/` folder and post what it prints. See
  [Script commands](#script-commands).

Template variables: `{{ user.username }}`, `{{ user.mention }}`, `{{ args }}`, `{{ argv }}`,
`{{ groups }}`, `{{ room.id }}`, `{{ message.text }}`, `{{ now }}`.

The **Try a message** box on the Commands page runs text through the live router.

### Handling many users at once

The bot answers up to **20 messages at the same time** (`APP_BOT_MAX_CONCURRENCY`, 1–200).
Inbound messages are sorted into lanes, one per user per room:

- Different users, including several in the same channel, are served in parallel. One
  user's slow script or API call doesn't hold anyone else up.
- One user's messages are answered **in the order they were sent**, one at a time, so a user
  can occupy at most one slot, and replies never arrive out of order.
- When all slots are busy, new messages wait for a free one; they aren't dropped.
- A user with more than 20 messages already waiting in a room has further messages dropped
  (logged as a warning), so a flood can't build an unbounded backlog.

The dashboard's **Handling now** row shows slots in use and messages waiting. Saving bot
settings that reconnect the bot (URLs, credentials, enabled) cancels in-flight replies,
including running scripts.

### Script commands

Put a script in `scripts/` at the repository root (`APP_SCRIPTS_DIR` to move it), then create
a command with response type **Run a script** and pick the file. Script commands always use a `prefix` keyword, so users can add
arguments: `hello one "two words"` runs `hello.py one "two words"`, and whatever it prints
becomes the reply.
[`scripts/README.md`](scripts/README.md) covers the details; two examples ship with it.

- `.py` files run with the bot's Python, `.sh` with bash, `.js` with node; anything else must
  be executable with a `#!` line.
- Arguments go to the script as argv, never through a shell. The message context arrives as
  JSON on stdin and as `BOT_*` environment variables.
- Exit code 0 → stdout is the reply (up to 4000 characters). Non-zero → the user sees a
  warning with the last line of stderr, and the log records an error.
- Each command has a timeout (30 s by default, 300 s max); the script and anything it started
  are killed when it runs out. Up to `APP_SCRIPT_MAX_CONCURRENCY` (20) scripts run at once.

Security model: **whoever can write to the scripts folder can run code as the bot.** The portal
can only bind a command to a file that is already there. It can't create or edit scripts. Scripts
get a minimal environment: the bot's secrets (`APP_SECRET_KEY`, the Rocket.Chat token) are
never passed on. Use `APP_SCRIPT_ENV_PASSTHROUGH` to hand specific variables, such as an API
key, to scripts. Arguments are typed by chat users, so scripts must treat them as untrusted.
Combine script commands with the [access policy](#access-who-can-use-the-bot) to decide who may
run them.

In Docker the folder is mounted read-only at `/app/scripts`, so edits on the host apply to the
next run without a rebuild. The image has Python and bash but not node.

### Access: who can use the bot

The portal's **Access** page sets one of three policies:

| Policy | Who the bot answers |
|---|---|
| Everyone (default) | anyone in the workspace |
| Only the users on the list | listed users; everyone else is refused |
| Everyone except the users on the list | everyone but the listed users |

Add users by Rocket.Chat username: paste several at once, separated by commas, spaces or new
lines. Matching ignores case and a leading `@`. Changes apply to the next message, with no
restart.

The bot never replies to a refused user: it ignores them silently, even when they message it
directly or address it in a channel. Refused messages show as `denied` on the Logs page. The check covers every command, built-ins included, so a refused
user can't create reminders either. Reminders they set up before being refused keep firing
until an admin pauses them on the Reminders page.

**Check a user** on the same page tells you whether the bot would answer someone, and why.
The **Try a message** box on the Commands page sends as `portal-tester`, so with an allowlist
it reports a refusal unless you add that name.

The list matches usernames, so a user who renames their Rocket.Chat account must be added
again under the new name.

### Reply style: whole or streaming

**Bot settings → Reply style** picks how replies appear in chat:

- **Whole message** (default): the finished reply is posted in one go.
- **Streaming**: the bot posts the start of the reply with a `▍` cursor, then edits that
  same message (`chat.update`) every *delay* ms, adding *characters per update* each time,
  until the full text is there. Cuts fall between words, and an unfinished code block is
  closed temporarily so the formatting doesn't flicker.

A streamed reply makes at most 40 edits and takes about 8 seconds at most; longer replies
grow in bigger chunks instead of taking longer, because a user's next message waits until
their current reply has finished streaming. Short replies that fit in one chunk are posted
whole. Reminders are always posted whole.

**Script commands stream live.** In streaming mode a script's output is typed out while it
runs, with the same *characters per update* and *delay* as any streamed reply: the bot posts
the start of the first line it prints, then reveals the next chunk every *delay* ms as long as
there is printed output left to show. If the script prints faster than that, chunks grow so
the message keeps up. When the script exits, whatever is left is typed out within the usual
40-edit / 8-second limits and the message becomes the final reply. If it fails or times out, what it printed stays and the
warning is added below it. Python scripts run unbuffered, so a plain `print()` shows up right
away; other languages should flush their output (`stdbuf -oL` for shell tools that buffer).

Rocket.Chat has no native streaming, so this depends on two workspace settings
(Administration → Settings → Message):

- **Allow Message Editing** must be on. If an edit is refused, the bot sends the full reply as
  a new message so the answer is never lost; the partial message stays above it.
- Turn **Show Edited Status** off if you don't want the *(edited)* mark on every streamed
  reply.

### API integrations

Each integration is a templated HTTP request plus a reply template. Parameters are declared in
order and filled from the command's arguments; the last one may be *greedy* (it swallows the
rest of the line). Reply templates see `{{ response.* }}` (parsed JSON), `{{ status }}` and
`{{ body }}`.

Example — `weather Hanoi`:

| Field | Value |
|---|---|
| Base URL | `https://api.open-meteo.com` |
| Path | `/v1/forecast` |
| Query | `{"latitude": "21.03", "longitude": "105.83", "current": "temperature_2m"}` |
| Reply | `Weather for {{ params.city }}: **{{ response.current.temperature_2m }}°C**` |
| Params | `city` (required) |

Secrets (bearer tokens, API keys, basic-auth passwords, the bot's own token) are encrypted at
rest with a key derived from `APP_SECRET_KEY` and are never returned by the API — the portal
only ever learns *whether* a secret is set.

### Reminders

From chat:

```
remind 09:00 stand-up notes                     every day
remind daily 09:00 stand-up notes               same thing, explicit
remind mon,fri 17:30 submit your timesheet      chosen weekdays
remind cron "*/15 9-17 * * 1-5" check the queue  cron
remind at 2026-10-20 09:00 one-off ping         a specific moment
remind in 30m take a break                      relative
remind daily 07:30 tz=Asia/Ho_Chi_Minh sync     explicit timezone
remind daily 09:00 stand-up #general            post to a channel

tasks               list yours
tasks off 3         pause        tasks on 3   resume
tasks delete 3      remove
```

Without `tz=`, reminders use the portal's **default timezone** setting. Asked in a channel
without naming one, the reminder posts back to that channel; asked in a DM, it comes back as a
DM. Users can only see and change their own reminders.

Admins get the full list in the portal, plus **Run now**, pause/resume and per-run history.

## Security notes

- Portal passwords are Argon2 hashes; sessions are JWTs signed with `APP_SECRET_KEY`.
- **Set `APP_SECRET_KEY` to a long random value.** Rotating it invalidates all sessions *and*
  makes stored secrets undecryptable — they must be re-entered.
- Reply/URL/body templates run in Jinja's sandbox, which blocks attribute escapes.
- Integration URLs are checked before each call: non-HTTP schemes are refused, and hosts that
  resolve to private, loopback or link-local addresses are blocked unless
  `APP_ALLOW_PRIVATE_INTEGRATION_HOSTS=true` (set it for local development only — it is what
  lets an integration reach something like `http://localhost:9000`).
- `viewer` accounts can read everything in the portal but change nothing.
- Every portal write is recorded in the audit log.

## Database

SQLite, at `backend/data/bot.db` (a Docker volume in the compose setup). A fresh install
creates its tables and stamps the current Alembic revision, so there is no migration step on
first run. After pulling a change that adds a migration:

```bash
cd backend && .venv/bin/alembic upgrade head
```

All instants are stored in UTC — SQLite silently drops timezone offsets, so `app/timeutil.py`
normalises on the way in and converts to the task's own timezone for display.

## Rocket.Chat stack (`rocketchat_server/`)

The Mongo healthcheck initiates the `rs0` replica set on an empty data folder, and Rocket.Chat
waits for it, so a fresh `docker compose up -d` works without the manual `rs.initiate` above
(it is harmless on a set that already exists). Mongo keeps its data in `./mongo_data`
(mounted at `/data/db`; the folder must belong to uid 1000).

## API

Interactive docs at `/docs` on the backend (`http://localhost:8000/docs` in development).
`GET /healthz` is unauthenticated and reports bot and scheduler state.

## LLM responder

A language model service built into the backend. With it on, configured commands still
answer first; any DM or @mention that matches none of them is answered by the model instead
of getting the fallback reply. Everything is set on the portal's **Language model** page:

| Setting | |
|---|---|
| Answer unmatched messages | On/off. Needs gateway, key and model. |
| API gateway | Any OpenAI-compatible chat-completions base URL, e.g. `https://localcal.ai/aigw/ai/v1`. Same private-host rule as integrations. |
| API key | Stored encrypted, never shown again. Leave blank to keep, clear to remove. |
| Model | Whichever model the gateway serves, e.g. `glm-4.6`. |
| Timeout | Seconds to wait for the gateway: for the whole answer, or when streaming, for the first token and between tokens. No retries, so this is the real wait. |
| System prompt | `{bot}` becomes the bot's username. Empty uses a built-in prompt. |
| Max tokens, temperature | Empty leaves the gateway's default. |
| History | Earlier messages of the conversation sent with each question (0 = none). |
| Format for Rocket.Chat | Rewrites the model's Markdown (see below). |

**Test** sends one question with the values on the form, saved or not, and shows the answer
as it would be posted. Changes apply to the next message; no restart is needed.

`APP_LLM_*` in `backend/.env` only seeds these settings when the database is first created.

### Rocket.Chat formatting

Models write standard Markdown, which Rocket.Chat renders differently (`*x*` is bold there,
tables and headings show as raw symbols). With formatting on, the answer is rewritten:
`**bold**` → `*bold*`, `*italic*` → `_italic_`, `~~x~~` → `~x~`, headings → bold lines,
`* item` → `- item`, tables → aligned columns in a code block, images → links, `<br>` → line
breaks, and a reasoning model's `<think>…</think>` is removed. Code and URLs are untouched.
The model is told to write standard Markdown and remembers its own words, not the rewrite.
Turn it off if your system prompt already asks for Rocket.Chat syntax.

### Streaming

With **Settings → Reply style** on streaming, the answer is typed out in the message as the
model produces it, using the same characters-per-update and delay settings as other replies.
Formatting is applied as it streams: unfinished `<think>` blocks and table rows are held back
until complete, so the message does not flicker between raw and formatted text.

- Each DM, thread or channel keeps its last few messages in memory so follow-ups make sense;
  this is lost on restart. In channels each question is prefixed with the sender's `@username`.
- Errors (timeout, HTTP status) are shown as a short warning and logged; the key never is.

### Integrations as tools

Tick **LLM tool** on an integration (Integrations page) and the model may call it on its own,
for questions like "what's the weather in Hanoi?". Its description and parameter descriptions
become the tool's documentation, so write them for the model. The call goes through the same
parameter binding, templates (`{{ user.username }}` is the person asking) and private-host guard
as a command; the JSON response, or the error, goes back to the model, which then answers.
At most 20 rounds of tool calls are made per answer (`APP_MAX_TOOL_ROUNDS` in the root `.env`, 1–100). Past them the model is told to stop and answers once more without tools, from the results it has.

Exposure is opt-in because the model decides when to call: anyone allowed to chat with the
bot can get it to call a ticked integration with arguments of their choosing. Leave write
endpoints (POST/DELETE) unticked unless that is acceptable.

The code lives in `app/bot/llm.py` (service), `app/bot/rcformat.py` (formatting) and
`app/api/llm.py` (portal API).

### Importing a folder (e.g. a Confluence space)

Knowledge base → **Import a folder** takes a whole folder as a ZIP upload (up to 1 GB) or from
`kb_import/` on the server (mounted read-only at `/app/kb_import`), as one *collection*. Markdown
written by `confluence-markdown-exporter` is read as it is laid out: titles from the H1, links as
their words, images, comments and the lock file skipped, attachments (optional) named after the
links to them. Importing again under the same name syncs: unchanged files are skipped, changed
ones replaced, removed ones removed. See DEPLOY.md §8.4. Code: `app/knowledge/folder.py`.

### Kept answers and caches

Questions looked up in the knowledge base can be answered again without the model, in well
under a second (**Cache** page):

- **Pinned answers**: a question, other ways of asking it, and the answer, written by an admin.
  Matched with case and spacing ignored, and by meaning when an embedding model is set up
  (*Similarity for pinned answers*, 0.9 by default).
- **Saved answers**: after a turn that answered from the knowledge base alone (no other tool,
  no documents sent in the conversation, passages the model judged to answer), the fast model
  rewrites the question to stand on its own and answers it from the passages only, so nothing of
  the person who asked is kept. The same question asked again (0.95 similarity by default) gets
  it, marked "⚡ Saved answer" with its sources. It goes when a knowledge-base document
  changes, when it expires (a week by default), or when someone reacts 👎 to it in chat; the
  question is then worked out afresh. A 👎 on a model answer keeps it from being saved.

Lookups (passages found and the model's choice among them) are kept 10 minutes in memory and
forgotten when a document changes. An integration whose **Reuse answers for** is set reuses
its last successful response for the same request; only set it on calls that read.

The code lives in `app/cache/` (answers and the in-memory cache) and `app/api/cache.py`.

### Jobs: work in several steps

A request that needs more than one reply ("compare these policies and write a summary") runs
as a **job**. The model starts one with its `start_job` tool, or a reply still being worked out
after *Go on as a job after* (45 s by default, Assistant page) is split into steps by the fast
model and handed over. Each step is a turn of its own with the same tools and the turn
deadline; its result is stored as it is done, so a restart goes on from the step under way. A
checklist in the conversation shows progress, and the answer is posted at the end and added to
the conversation. People can keep chatting meanwhile.

In chat: `job list`, `job status <id>`, `job cancel <id>` (people see only their own jobs).
Admins see every job, with each step's output, on the **Jobs** page. Two jobs run at once; each
person may have two under way (configurable), with up to six steps each.

The code lives in `app/agent/jobs.py` and `app/api/jobs.py`.

### Tools by profile, MCP servers, skills

**Who gets which tool** (Access → *Tools by profile*): people listed as trusted get the
*trusted* profile, everyone else *safe*. A tool is offered when its name matches an allow glob
and no deny glob. By default safe people get every tool except MCP tools (`mcp__*`) and
running skill scripts (`skill_run`); trusted people get them all. An MCP tool an admin opened to
*everyone* on the MCP servers page is offered to both profiles whatever the globs say. In a
channel the profile is the sender's; a job uses the profile of the person who started it.

**MCP servers** (MCP servers page): the bot is an MCP client (`mcp` SDK). A server is either a
Streamable HTTP URL with headers (stored encrypted; the private-network guard applies, and a
server on the host is `http://host.docker.internal:<port>/mcp`) or a command run in the backend
container over stdio. A stdio server runs in `mcp_servers/` (mounted read-only at
`/app/mcp_servers`), and `python` is the bot's own interpreter, so a Python server kept there is
set up as command `python`, arguments `<folder>/server.py` and can import the `mcp` SDK; see
`mcp_servers/README.md` and `mcp_servers/_example`. The image has no `node`/`npx`. *Refresh tools*
lists its tools, which start **off**, with what the server says of each (read-only,
destructive); turn on the ones the model may call. They are offered as `mcp__<server>__<tool>`.
Per tool, an admin can also open it to everyone and have its answers reused for the same
arguments (*Reuse*, seconds). A server's session (a stdio server's process) is kept open for the
next call until it has been idle *Keep alive* seconds (300; 0 opens one per call); a session idle
for 30 s is pinged first, and one that fails is replaced, with a read-only tool's call retried on
the new one. Each call is bounded by the server's timeout.

**Skills** (Skills page): folders in `skills/` (mounted read-only at `/app/skills`), each with a
`SKILL.md` whose front matter gives `name`, `description` and optional `requires.bins` /
`requires.env`. The model sees the list in its `skill_read` tool, reads a skill before following
it, and runs the skill's `scripts/` with `skill_run` (45 s at most, like script commands). A skill
whose requirements are missing, or that an admin turned off, is not offered. See
`skills/_example`.

The code lives in `app/agent/policy.py`, `app/integrations/mcp.py`, `app/agent/tools/mcp.py`,
`app/agent/skills.py` and `app/agent/tools/skills.py`.
