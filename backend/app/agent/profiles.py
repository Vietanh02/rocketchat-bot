"""What the assistant knows about each person it talks to.

A profile holds how someone likes to be called, the language and timezone
to answer in, and a few free words about them. It is shown to the model in
that person's DMs only; in a channel, everyone would read what it says.
Admins edit profiles in the portal, and people edit their own through the
model (`profile_update`, offered in DMs).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AgentUserProfile
from app.scheduler.triggers import ScheduleError, resolve_timezone

# Profiles kept in memory; a busy bot reads each once.
HOT_PROFILES = 1000
FIELDS = ("display_name", "language", "timezone", "about")
MAX_LENGTH = {"display_name": 128, "language": 64, "timezone": 64, "about": 1000}


class ProfileError(ValueError):
    """A field value was refused; the message says why."""


@dataclass(slots=True, frozen=True)
class Profile:
    rc_user_id: str
    username: str = ""
    display_name: str = ""
    language: str = ""
    timezone: str = ""
    about: str = ""

    @classmethod
    def from_row(cls, row: AgentUserProfile) -> Profile:
        return cls(
            row.rc_user_id, row.username, row.display_name, row.language, row.timezone, row.about
        )

    @property
    def empty(self) -> bool:
        return not any(getattr(self, field) for field in FIELDS)

    def prompt_text(self) -> str:
        """What the model is told about the person, or "" if nothing is known."""
        if self.empty:
            return ""
        who = f"@{self.username}" if self.username else "the user"
        lines = [f"About {who}, who you are talking with:"]
        if self.display_name:
            lines.append(f"- Call them: {self.display_name}")
        if self.language:
            lines.append(f"- Answer in: {self.language}")
        if self.timezone:
            lines.append(f"- Their timezone: {self.timezone}")
        if self.about:
            lines.append(f"- {self.about}")
        return "\n".join(lines)


def clean_fields(fields: dict[str, str | None]) -> dict[str, str]:
    """The given fields, trimmed and checked; raises ProfileError."""
    cleaned: dict[str, str] = {}
    for name, value in fields.items():
        if name not in FIELDS or value is None:
            continue
        value = " ".join(str(value).split()) if name != "about" else str(value).strip()
        if len(value) > MAX_LENGTH[name]:
            raise ProfileError(f"{name} may be at most {MAX_LENGTH[name]} characters")
        if name == "timezone" and value:
            try:
                resolve_timezone(value)
            except ScheduleError as exc:
                raise ProfileError(
                    f"{value!r} is not a timezone name such as Asia/Ho_Chi_Minh"
                ) from exc
        cleaned[name] = value
    return cleaned


class ProfileStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._hot: OrderedDict[str, Profile | None] = OrderedDict()

    def _remember(self, rc_user_id: str, profile: Profile | None) -> None:
        self._hot[rc_user_id] = profile
        self._hot.move_to_end(rc_user_id)
        while len(self._hot) > HOT_PROFILES:
            self._hot.popitem(last=False)

    def forget(self, rc_user_id: str | None = None) -> None:
        if rc_user_id is None:
            self._hot.clear()
        else:
            self._hot.pop(rc_user_id, None)

    async def get(self, rc_user_id: str) -> Profile | None:
        if rc_user_id in self._hot:
            self._hot.move_to_end(rc_user_id)
            return self._hot[rc_user_id]
        async with self._session_factory() as db:
            row = await db.scalar(
                select(AgentUserProfile).where(AgentUserProfile.rc_user_id == rc_user_id)
            )
        profile = Profile.from_row(row) if row is not None else None
        self._remember(rc_user_id, profile)
        return profile

    async def update(self, rc_user_id: str, username: str, **fields: str | None) -> Profile:
        """Set the given fields, creating the profile on first use."""
        cleaned = clean_fields(fields)
        async with self._session_factory() as db:
            row = await db.scalar(
                select(AgentUserProfile).where(AgentUserProfile.rc_user_id == rc_user_id)
            )
            if row is None:
                row = AgentUserProfile(rc_user_id=rc_user_id, username=username or "")
                db.add(row)
            elif username:
                row.username = username
            for name, value in cleaned.items():
                setattr(row, name, value)
            await db.commit()
            await db.refresh(row)
            profile = Profile.from_row(row)
        self._remember(rc_user_id, profile)
        return profile
