"""Timezone handling at the storage boundary.

SQLite has no timezone type: a tz-aware datetime goes in and comes back naive
with the offset silently dropped. So every instant we persist is converted to
UTC first, and every instant we read back is read as UTC. Display converts to
the owning task's timezone at the last moment.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.scheduler.triggers import ScheduleError, resolve_timezone


def to_utc(value: datetime | None) -> datetime | None:
    """Normalise an instant for storage. Naive input is assumed to be UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def read_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a value loaded from the database."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def in_timezone(value: datetime | None, timezone_name: str | None) -> datetime | None:
    """Convert a stored instant into a named timezone for display."""
    stored = read_utc(value)
    if stored is None:
        return None
    try:
        return stored.astimezone(resolve_timezone(timezone_name))
    except ScheduleError:
        return stored


def format_local(value: datetime | None, timezone_name: str | None) -> str:
    local = in_timezone(value, timezone_name)
    if local is None:
        return "not scheduled"
    return f"{local:%Y-%m-%d %H:%M %Z}".strip()
