"""`skill_read` and `skill_run`: following the skills in the skills folder.

`skill_read`'s description lists the skills offered (name and what each is
for), so the model knows them without reading every one. A skill's scripts
run like script commands (`app.bot.scripts`): a minimal environment, the
message as JSON on stdin, a timeout, and their output clipped.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.agent.skills import SCRIPTS, Skill, SkillError, read_file
from app.agent.tools import tool_result
from app.bot.scripts import ScriptError, run_script

log = logging.getLogger(__name__)

READ, RUN = "skill_read", "skill_run"
# A skill script never runs longer than this.
RUN_TIMEOUT_SECONDS = 45.0
MAX_ARGS = 20
MAX_OUTPUT_CHARS = 6000


class SkillTools:
    def __init__(self, skills: Sequence[Skill], context: dict[str, Any]) -> None:
        self._skills = {skill.name: skill for skill in skills}
        # What a script gets on stdin, as for script commands.
        self._context = context

    def tools(self) -> dict[str, dict[str, Any]]:
        if not self._skills:
            return {}
        catalog = "\n".join(f"- {s.name}: {s.description}" for s in self._skills.values())
        names = list(self._skills)
        tools: dict[str, dict[str, Any]] = {
            READ: {
                "description": (
                    "Read a skill: instructions for a kind of task, written by the admins. When a "
                    "request matches one, read its SKILL.md first and follow it. Skills:\n" + catalog
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": names},
                        "file": {
                            "type": "string",
                            "description": "A file of the skill the instructions mention; default SKILL.md.",
                        },
                    },
                    "required": ["name"],
                },
            }
        }
        runnable = [s.name for s in self._skills.values() if s.scripts]
        if runnable:
            tools[RUN] = {
                "description": (
                    "Run a script of a skill, as its SKILL.md says, and get its output. "
                    "Scripts are listed when you read the skill."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": runnable},
                        "script": {"type": "string", "description": "Its path under scripts/."},
                        "args": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "script"],
                },
            }
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        skill = self._skills.get(str(arguments.get("name") or ""))
        if skill is None:
            return tool_result({"error": f"there is no skill {arguments.get('name')!r}"})
        if name == READ:
            return self._read(skill, str(arguments.get("file") or ""))
        return await self._run(skill, arguments)

    def _read(self, skill: Skill, file: str) -> str:
        try:
            text = read_file(skill, file or "SKILL.md")
        except (SkillError, OSError) as exc:
            return tool_result({"error": str(exc)})
        payload: dict[str, Any] = {"skill": skill.name, "file": file or "SKILL.md", "text": text}
        if not file:
            payload["files"] = skill.files
            payload["scripts"] = skill.scripts
        return tool_result(payload)

    async def _run(self, skill: Skill, arguments: dict[str, Any]) -> str:
        script = str(arguments.get("script") or "").removeprefix(f"{SCRIPTS}/")
        if script not in skill.scripts:
            return tool_result({"error": f"{skill.name} has no script {script!r}", "scripts": skill.scripts})
        raw = arguments.get("args") or []
        args = [str(a) for a in raw][:MAX_ARGS] if isinstance(raw, list) else []
        try:
            result = await run_script(
                script,
                args=args,
                context=self._context,
                timeout=RUN_TIMEOUT_SECONDS,
                root=skill.path / SCRIPTS,
            )
        except ScriptError as exc:
            return tool_result({"error": str(exc)})
        log.info(
            "Skill %s ran %s: exit %s in %d ms",
            skill.name,
            script,
            "timeout" if result.timed_out else result.exit_code,
            result.duration_ms,
        )
        return tool_result(
            {
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stdout": result.stdout[-MAX_OUTPUT_CHARS:],
                "stderr": result.stderr[-2000:],
            }
        )
