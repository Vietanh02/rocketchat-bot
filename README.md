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

The portal is then at <http://localhost:8080>. Log in with
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
cd backend && .venv/bin/python -m pytest -q      # 317 tests, no network or Rocket.Chat needed
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

## Known issues in `rocketchat_server/docker-compose.yml`

Two problems in the pre-existing Rocket.Chat stack, found while testing against it:

1. **The replica set is never initiated.** Rocket.Chat crash-loops with
   `Topology is closed` until you run the `rs.initiate(...)` command in step 2 above. Worth
   adding as a one-shot init service so a fresh `up` just works.

2. **MongoDB's data is not actually persisted.** The compose file mounts
   `./mongo_data:/var/lib/mongo`, but this image stores its data in `/data/db` — the host
   directory stays empty and everything lives in the container's writable layer, so
   `docker compose down` destroys the workspace. The fix is:

   ```yaml
   volumes:
     - ./mongo_data:/data/db
   ```

   Changing it points Mongo at an empty directory, so the existing workspace would look
   freshly installed. Copy the data across first if you want to keep it:

   ```bash
   docker compose cp mongodb:/data/db/. ./mongo_data/
   ```

   This was left unchanged because fixing it in place is destructive — it is your call.

## API

Interactive docs at `/docs` on the backend (`http://localhost:8000/docs` in development).
`GET /healthz` is unauthenticated and reports bot and scheduler state.

## Extending: an LLM responder

The responder is pluggable. `RuleResponder` in `app/bot/responder.py` implements the
`Responder` protocol (`async def respond(message) -> Reply | None`); an `LLMResponder` can be
added alongside it and selected via `bot_config.responder_mode` without a schema change.
`IntegrationSpec.to_tool_schema()` already renders each configured integration as a Claude
tool definition, so the integrations an admin has configured become the model's tools.
