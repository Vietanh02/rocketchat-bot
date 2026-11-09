---
name: example
description: An example skill. Copy this folder to a name without the leading "_" to offer it.
requires:
  bins: [python3]
  env: []
---

# Example skill

Folders whose names start with `_` or `.` are never offered, so this one is only
documentation. A skill is a folder holding:

- `SKILL.md`: YAML front matter (`name`, `description`, optional `requires.bins` and
  `requires.env`), then instructions written for the assistant. The description is
  what the assistant sees in its list of skills, so say when to use the skill.
- `scripts/`: optional programs the instructions tell it to run with `skill_run`.
  They get the message as JSON on stdin and `BOT_*` environment variables, like
  script commands, and run for at most 45 seconds.
- any other file the instructions mention (templates, examples); the assistant
  reads them with `skill_read`.

## Instructions

When asked to greet someone formally, run `scripts/hello.py` with their name as the
only argument and reply with its output.
