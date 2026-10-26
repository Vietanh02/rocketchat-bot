"""Translate a ScheduledTask row into an APScheduler trigger."""

from __future__ import annotations

from datetime import datetime, timezone as _timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.models import ScheduledTask, ScheduleKind

UTC = _timezone.utc

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ScheduleError(ValueError):
    """The task's schedule fields are inconsistent or unparseable."""


def resolve_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown timezone {name!r}") from exc


def parse_time_of_day(value: str | None) -> tuple[int, int]:
    """Accept "HH:MM" (also "H:MM" and "HHMM")."""
    if not value:
        raise ScheduleError("time_of_day is required (HH:MM)")
    raw = value.strip()
    if ":" not in raw and raw.isdigit() and len(raw) in (3, 4):
        raw = f"{raw[:-2]}:{raw[-2:]}"
    try:
        hour_str, minute_str = raw.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except ValueError as exc:
        raise ScheduleError(f"cannot parse time {value!r}; expected HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"time out of range: {value!r}")
    return hour, minute


def normalize_time_of_day(value: str | None) -> str:
    """Canonical "HH:MM" form, for storage and display."""
    hour, minute = parse_time_of_day(value)
    return f"{hour:02d}:{minute:02d}"


def normalize_days(value: str | None) -> str:
    """Normalise "Mon, tuesday" to APScheduler's "mon,tue"."""
    if not value or not value.strip():
        return ",".join(WEEKDAYS)
    days: list[str] = []
    for token in value.replace(";", ",").split(","):
        key = token.strip().casefold()[:3]
        if not key:
            continue
        if key not in WEEKDAYS:
            raise ScheduleError(f"unknown day of week: {token.strip()!r}")
        if key not in days:
            days.append(key)
    if not days:
        raise ScheduleError("no valid days of week given")
    return ",".join(sorted(days, key=WEEKDAYS.index))


def build_trigger(task: ScheduledTask) -> BaseTrigger:
    tz = resolve_timezone(task.timezone_name)
    kind = task.schedule_kind

    if kind == ScheduleKind.daily.value:
        hour, minute = parse_time_of_day(task.time_of_day)
        return CronTrigger(hour=hour, minute=minute, timezone=tz)

    if kind == ScheduleKind.weekly.value:
        hour, minute = parse_time_of_day(task.time_of_day)
        return CronTrigger(
            day_of_week=normalize_days(task.days_of_week),
            hour=hour,
            minute=minute,
            timezone=tz,
        )

    if kind == ScheduleKind.cron.value:
        if not task.cron_expr:
            raise ScheduleError("cron_expr is required for cron schedules")
        try:
            return CronTrigger.from_crontab(task.cron_expr.strip(), timezone=tz)
        except ValueError as exc:
            raise ScheduleError(f"invalid cron expression: {exc}") from exc

    if kind == ScheduleKind.once.value:
        if not task.run_at:
            raise ScheduleError("run_at is required for one-off schedules")
        # Stored instants are UTC (see app/timeutil.py); the trigger's own
        # timezone only affects how APScheduler reports the fire time.
        run_at = task.run_at
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=UTC)
        return DateTrigger(run_date=run_at, timezone=tz)

    raise ScheduleError(f"unknown schedule kind: {kind!r}")


def describe(task: ScheduledTask) -> str:
    """Human-readable schedule, for chat replies and the portal list."""
    kind = task.schedule_kind
    tz = task.timezone_name or "UTC"
    if kind == ScheduleKind.daily.value:
        return f"daily at {normalize_time_of_day(task.time_of_day)} ({tz})"
    if kind == ScheduleKind.weekly.value:
        return (
            f"{normalize_days(task.days_of_week)} at "
            f"{normalize_time_of_day(task.time_of_day)} ({tz})"
        )
    if kind == ScheduleKind.cron.value:
        return f"cron `{task.cron_expr}` ({tz})"
    if kind == ScheduleKind.once.value:
        return f"once at {_local_run_at(task)} ({tz})"
    return kind


def _local_run_at(task: ScheduledTask) -> str:
    """run_at is stored in UTC; show it in the task's own timezone."""
    if not task.run_at:
        return "?"
    stored = task.run_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    try:
        local = stored.astimezone(resolve_timezone(task.timezone_name))
    except ScheduleError:
        local = stored
    return f"{local:%Y-%m-%d %H:%M}"


def next_fire_time(task: ScheduledTask, *, after: datetime | None = None) -> datetime | None:
    """Compute the next run without registering the job."""
    trigger = build_trigger(task)
    tz = resolve_timezone(task.timezone_name)
    now = after or datetime.now(tz)
    return trigger.get_next_fire_time(None, now)
