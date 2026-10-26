# Bot scripts

Every runnable file in this folder can be bound to a chat command in the portal
(Commands → Response type **Run a script**). The bot runs it and posts what it
prints.

| File | Runs with |
|---|---|
| `*.py` | the bot's own Python (so `httpx` etc. are available) |
| `*.sh` | `bash` |
| `*.js` | `node`, if installed |
| anything else | directly; it must be executable (`chmod +x`) and start with a `#!` line |

Files and folders starting with `.` or `_` are never offered, so keep helpers in
`_lib/` or similar. Subfolders are fine: `deploy/status.sh`.

## What a script receives

- **Arguments**: the words after the command, split like a shell would
  (`hello one "two words"` → `one`, `two words`; in a channel,
  `@taskbot hello one "two words"`). They are passed directly,
  never through a shell, but they are still typed by chat users: treat them as
  untrusted input.
- **Standard input**: JSON with `command`, `args`, `args_text`, `groups`
  (regex named groups), `user`, `room`, `message` and `now`.
- **Environment**: `BOT_COMMAND`, `BOT_ARGS`, `BOT_USERNAME`, `BOT_USER_ID`,
  `BOT_ROOM_ID`, `BOT_ROOM_TYPE`, `BOT_MESSAGE_ID`, `BOT_MESSAGE_TEXT`, and
  `BOT_GROUP_<NAME>` per regex group. Plus `PATH`, `HOME`, `LANG` and `TZ`.
  Nothing else from the bot's environment is passed on, so its secrets stay
  private. To give scripts an API key, list the variable in
  `APP_SCRIPT_ENV_PASSTHROUGH` (comma-separated).
- **Working directory**: the script's own folder.

## What the bot does with the result

- Exit code 0: stdout becomes the reply (up to 4000 characters; Markdown works).
  No output → `✅ Done.`
- Non-zero exit: the user sees `⚠️ <script> failed (exit code N): <last stderr line>`.
- Over the command's timeout (30 s by default, 300 s max): the script and
  anything it started are killed.

Several users can run scripts at once (up to `APP_SCRIPT_MAX_CONCURRENCY`, 20 by
default), so a script may run in parallel with another copy of itself. Avoid writing
to a shared file without locking it.
