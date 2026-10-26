"""Who may use the bot.

The policy is part of the cached config snapshot, so checking it costs
nothing per message. Users are matched by Rocket.Chat username, compared
case-insensitively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import AccessMode

# Rocket.Chat's default username rule, plus unicode letters for workspaces
# that enable UTF-8 usernames.
USERNAME_RE = re.compile(r"^[\w.\-]{1,128}$")


def normalize_username(value: str) -> str:
    """'@Bo ' -> 'bo'. Raises ValueError for something that cannot be one."""
    username = value.strip().lstrip("@").casefold()
    if not USERNAME_RE.match(username):
        raise ValueError(f"{value.strip()!r} is not a valid Rocket.Chat username")
    return username


@dataclass(slots=True, frozen=True)
class AccessPolicy:
    mode: str = AccessMode.everyone.value
    usernames: frozenset[str] = frozenset()

    def allows(self, username: str | None) -> bool:
        if self.mode == AccessMode.everyone.value:
            return True
        listed = bool(username) and username.casefold() in self.usernames
        if self.mode == AccessMode.allowlist.value:
            return listed
        return not listed
