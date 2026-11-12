"""Which tools the model may use, depending on who is asking.

Two profiles (`app.models.ToolPolicy`):

- **trusted**: the usernames an admin lists;
- **safe**: everyone else.

A tool is offered when its name matches one of the profile's allow globs
and none of its deny globs. Out of the box, safe users get every tool
except MCP tools (`mcp__*`) and running skill scripts (`skill_run`); trusted
users get them all. An MCP tool an admin opened to everyone (`open_tools`)
is offered to both profiles whatever the globs say. In a channel the profile is the sender's, so a trusted
person's tools are never offered to someone else's message; a job uses the
profile of the person who started it.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

SAFE, TRUSTED = "safe", "trusted"


def globs(text: str) -> tuple[str, ...]:
    """One glob per line (or comma); blank lines and `#` comments are skipped."""
    out: list[str] = []
    for line in text.replace(",", "\n").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and line not in out:
            out.append(line)
    return tuple(out)


def usernames(text: str) -> frozenset[str]:
    return frozenset(name.lstrip("@").casefold() for name in globs(text))


@dataclass(slots=True, frozen=True)
class ToolPolicySettings:
    trusted: frozenset[str] = frozenset()
    safe_allow: tuple[str, ...] = ("*",)
    safe_deny: tuple[str, ...] = ("mcp__*", "skill_run")
    trusted_allow: tuple[str, ...] = ("*",)
    trusted_deny: tuple[str, ...] = ()
    # Tool names offered to everyone regardless of the globs.
    open_tools: frozenset[str] = frozenset()

    @classmethod
    def from_model(cls, model, *, open_tools: frozenset[str] = frozenset()) -> ToolPolicySettings:
        return cls(
            open_tools=open_tools,
            trusted=usernames(model.trusted_usernames or ""),
            safe_allow=globs(model.safe_allow or ""),
            safe_deny=globs(model.safe_deny or ""),
            trusted_allow=globs(model.trusted_allow or ""),
            trusted_deny=globs(model.trusted_deny or ""),
        )

    def profile(self, username: str | None) -> str:
        return TRUSTED if username and username.lstrip("@").casefold() in self.trusted else SAFE

    def permits(self, profile: str, tool: str) -> bool:
        if tool in self.open_tools:
            return True
        allow, deny = (
            (self.trusted_allow, self.trusted_deny)
            if profile == TRUSTED
            else (self.safe_allow, self.safe_deny)
        )
        return any(fnmatchcase(tool, g) for g in allow) and not any(fnmatchcase(tool, g) for g in deny)

    def for_user(self, username: str | None):
        """A check of tool names for this person: `permit(name) -> bool`."""
        profile = self.profile(username)
        return lambda tool: self.permits(profile, tool)
