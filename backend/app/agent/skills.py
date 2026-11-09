"""Skills: instructions (and scripts) the assistant can follow.

Each skill is a folder in the skills folder (`APP_SKILLS_DIR`, mounted
read-only in the container):

    skills/
      release-notes/
        SKILL.md          front matter, then the instructions
        scripts/          optional; run with the `skill_run` tool
          collect.py
        template.md       any other file the instructions mention

SKILL.md starts with YAML front matter:

    ---
    name: release-notes
    description: Write release notes from the merged pull requests.
    requires:
      bins: [git]          # programs that must be installed
      env: [GITHUB_TOKEN]  # environment variables that must be set
    ---

A skill whose requirements are missing, or that an admin turned off, is
not offered. The model sees the list of skills (name and description) in
the `skill_read` tool, reads a skill's instructions before following them,
and runs its scripts with `skill_run` (by default only for trusted users,
see `app.agent.policy`).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models import SkillState

log = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
SCRIPTS = "scripts"
# A file read for the model is clipped to this.
MAX_READ_CHARS = 40_000
MAX_DESCRIPTION_CHARS = 500
# How long the folder and the on/off states are kept before looking again.
CATALOG_TTL_SECONDS = 30.0
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


class SkillError(ValueError):
    """A skill or one of its files could not be used; the message says why."""


@dataclass(slots=True)
class Skill:
    # The folder's name: how the model and the portal refer to it.
    name: str
    path: Path
    title: str = ""
    description: str = ""
    requires_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    # What is missing for it to work ("git not installed", ...).
    missing: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    enabled: bool = True
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.enabled and not self.missing and self.error is None


def skills_root() -> Path:
    return Path(get_settings().skills_dir).expanduser().resolve()


def front_matter(text: str) -> tuple[dict, str]:
    """The YAML front matter of a SKILL.md, and the rest."""
    match = _FRONT.match(text)
    if match is None:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"its front matter is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise SkillError("its front matter is not a mapping")
    return data, text[match.end() :]


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return []


def _listing(folder: Path) -> tuple[list[str], list[str]]:
    """(scripts, other files) of a skill, as paths relative to its folder."""
    scripts: list[str] = []
    files: list[str] = []
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if not path.is_file() or any(p.startswith((".", "_")) for p in relative.parts):
            continue
        if relative.parts[0] == SCRIPTS and len(relative.parts) > 1:
            scripts.append(Path(*relative.parts[1:]).as_posix())
        else:
            files.append(relative.as_posix())
        if len(scripts) + len(files) > 200:
            break
    return scripts, files


def load_skill(folder: Path) -> Skill:
    skill = Skill(name=folder.name, path=folder)
    try:
        data, _ = front_matter((folder / SKILL_FILE).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SkillError) as exc:
        skill.error = f"cannot read {SKILL_FILE}: {exc}"
        return skill
    skill.title = str(data.get("name") or folder.name).strip()
    skill.description = " ".join(str(data.get("description") or "").split())[:MAX_DESCRIPTION_CHARS]
    requires = data.get("requires") if isinstance(data.get("requires"), dict) else {}
    skill.requires_bins = _strings(requires.get("bins"))
    skill.requires_env = _strings(requires.get("env"))
    skill.missing = [f"`{b}` is not installed" for b in skill.requires_bins if shutil.which(b) is None]
    skill.missing += [f"`{e}` is not set" for e in skill.requires_env if not os.environ.get(e)]
    if not skill.description:
        skill.missing.append("its SKILL.md has no description")
    skill.scripts, skill.files = _listing(folder)
    return skill


def scan(root: Path | None = None) -> list[Skill]:
    """Every skill folder (one with a SKILL.md) in the skills folder."""
    root = root or skills_root()
    if not root.is_dir():
        return []
    return [
        load_skill(folder)
        for folder in sorted(root.iterdir())
        if folder.is_dir() and not folder.name.startswith((".", "_")) and (folder / SKILL_FILE).is_file()
    ]


def read_file(skill: Skill, relative: str = SKILL_FILE) -> str:
    """A text file of the skill, refusing paths that leave its folder."""
    path = Path(relative or SKILL_FILE)
    if path.is_absolute() or ".." in path.parts:
        raise SkillError(f"{relative!r} is not a file of the skill")
    full = (skill.path / path).resolve()
    if not full.is_relative_to(skill.path.resolve()) or not full.is_file():
        raise SkillError(f"the skill has no file {relative!r}")
    data = full.read_bytes()
    if b"\0" in data[:4096]:
        raise SkillError(f"{relative!r} is not a text file")
    text = data.decode("utf-8", errors="replace")
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + "\n…(truncated)"
    return text


class SkillCatalog:
    """The skills found, with the on/off state admins set on the portal."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._cached: tuple[float, list[Skill]] | None = None

    def invalidate(self) -> None:
        self._cached = None

    async def all(self) -> list[Skill]:
        now = time.monotonic()
        if self._cached is not None and now < self._cached[0]:
            return self._cached[1]
        skills = scan()
        async with self._session_factory() as db:
            states = {row.name: row.enabled for row in (await db.scalars(select(SkillState))).all()}
        for skill in skills:
            skill.enabled = states.get(skill.name, True)
        self._cached = (now + CATALOG_TTL_SECONDS, skills)
        return skills

    async def available(self) -> list[Skill]:
        return [skill for skill in await self.all() if skill.available]

    async def set_enabled(self, name: str, enabled: bool) -> None:
        async with self._session_factory() as db:
            row = await db.get(SkillState, name)
            if row is None:
                db.add(SkillState(name=name, enabled=enabled))
            else:
                row.enabled = enabled
            await db.commit()
        self.invalidate()
