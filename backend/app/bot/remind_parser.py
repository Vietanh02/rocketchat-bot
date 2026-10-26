"""Parses the chat syntax for creating reminders.

Accepted forms (the leading `remind` is stripped before we get here):

    09:00 stand-up notes                  -> daily
    daily 09:00 stand-up notes            -> daily
    weekly mon,fri 09:00 weekly report    -> weekly
    mon,fri 09:00 weekly report           -> weekly
    cron "*/15 9-17 * * 1-5" check queue  -> cron
    at 2026-10-20 09:00 one-off ping      -> once
    in 30m take a break                   -> once
    ... tz=Asia/Ho_Chi_Minh               -> any of the above, explicit timezone
    ... #general                          -> deliver to a channel instead of a DM
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import ScheduleKind
from app.scheduler.triggers import (
    ScheduleError,
    normalize_days,
    normalize_time_of_day,
    resolve_timezone,
)

# A colon is required: it is what separates "9:5" (a time) from "100"
# (which in "100 pushups" is not a time at all).
TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]?\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DURATION_RE = re.compile(r"^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", re.I)
TZ_RE = re.compile(r"(?:^|\s)tz=(\S+)", re.I)
CHANNEL_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9._-]+)")
# Exact weekday spellings only, so a message like "monitor the queue" is not
# mistaken for a Monday schedule.
DAY_WORDS = frozenset(
    {
        "mon", "monday", "tue", "tues", "tuesday", "wed", "weds", "wednesday",
        "thu", "thur", "thurs", "thursday", "fri", "friday",
        "sat", "saturday", "sun", "sunday",
    }
)

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


class RemindParseError(ValueError):
    """The reminder text could not be understood."""


@dataclass(slots=True)
class ParsedReminder:
    schedule_kind: str
    message: str
    timezone_name: str
    time_of_day: str | None = None
    days_of_week: str | None = None
    cron_expr: str | None = None
    run_at: datetime | None = None
    channel_name: str | None = None

    @property
    def title(self) -> str:
        first_line = self.message.strip().splitlines()[0] if self.message.strip() else "Reminder"
        return first_line[:120]


def _parse_duration(token: str) -> timedelta | None:
    match = DURATION_RE.match(token.strip())
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).casefold()[0]
    return timedelta(seconds=amount * _UNIT_SECONDS[unit])


def parse_reminder(text: str, *, default_timezone: str = "UTC") -> ParsedReminder:
    raw = (text or "").strip()
    if not raw:
        raise RemindParseError("Tell me when and what to remind you about.")

    # Pull out the modifiers that can appear anywhere.
    timezone_name = default_timezone
    tz_match = TZ_RE.search(raw)
    if tz_match:
        timezone_name = tz_match.group(1)
        raw = (raw[: tz_match.start()] + " " + raw[tz_match.end() :]).strip()
    try:
        tz = resolve_timezone(timezone_name)
    except ScheduleError as exc:
        raise RemindParseError(str(exc)) from exc

    channel_name = None
    channel_match = CHANNEL_RE.search(raw)
    if channel_match:
        channel_name = channel_match.group(1)
        raw = (raw[: channel_match.start()] + " " + raw[channel_match.end() :]).strip()

    if not raw:
        raise RemindParseError("Tell me when and what to remind you about.")

    tokens = raw.split()
    head = tokens[0].casefold()

    if head == "cron":
        return _parse_cron(raw[len(tokens[0]) :].strip(), timezone_name, channel_name)
    if head == "at":
        return _parse_once(tokens[1:], timezone_name, channel_name, tz)
    if head == "in":
        return _parse_relative(tokens[1:], timezone_name, channel_name, tz)
    if head == "daily":
        return _parse_daily(tokens[1:], timezone_name, channel_name)
    if head == "weekly":
        return _parse_weekly(tokens[1:], timezone_name, channel_name)

    # No keyword: a day list means weekly, a bare time means daily.
    if _looks_like_days(tokens[0]):
        return _parse_weekly(tokens, timezone_name, channel_name)
    if TIME_RE.match(tokens[0]):
        return _parse_daily(tokens, timezone_name, channel_name)

    raise RemindParseError(
        "I could not find a time. Try `daily 09:00 stand-up`, "
        "`mon,fri 09:00 report`, `in 30m stretch`, or "
        '`cron "*/15 * * * *" ping`.'
    )


def _looks_like_days(token: str) -> bool:
    """True when the token is a weekday, or the start of a weekday list."""
    candidates = [part for part in token.casefold().split(",") if part]
    return bool(candidates) and all(part in DAY_WORDS for part in candidates)


def _require_message(parts: list[str], what: str) -> str:
    message = " ".join(parts).strip()
    if not message:
        raise RemindParseError(f"What should I say when the {what} fires?")
    return message


def _parse_daily(tokens: list[str], tz_name: str, channel: str | None) -> ParsedReminder:
    if not tokens or not TIME_RE.match(tokens[0]):
        raise RemindParseError("Expected a time like `09:00` after `daily`.")
    return ParsedReminder(
        schedule_kind=ScheduleKind.daily.value,
        time_of_day=normalize_time_of_day(tokens[0]),
        message=_require_message(tokens[1:], "reminder"),
        timezone_name=tz_name,
        channel_name=channel,
    )


def _parse_weekly(tokens: list[str], tz_name: str, channel: str | None) -> ParsedReminder:
    # The day list may have been written with spaces: "mon, fri 09:00 ...".
    joined = " ".join(tokens)
    match = re.match(
        r"^((?:[A-Za-z]+\s*,\s*)*[A-Za-z]+)\s+((?:[01]?\d|2[0-3]):[0-5]?\d)\s*(.*)$",
        joined,
        re.S,
    )
    if not match:
        raise RemindParseError(
            "Expected days and a time, e.g. `weekly mon,fri 09:00 report`."
        )
    try:
        days = normalize_days(match.group(1))
    except ScheduleError as exc:
        raise RemindParseError(str(exc)) from exc
    return ParsedReminder(
        schedule_kind=ScheduleKind.weekly.value,
        days_of_week=days,
        time_of_day=normalize_time_of_day(match.group(2)),
        message=_require_message([match.group(3)], "reminder"),
        timezone_name=tz_name,
        channel_name=channel,
    )


def _parse_cron(rest: str, tz_name: str, channel: str | None) -> ParsedReminder:
    rest = rest.strip()
    if not rest:
        raise RemindParseError('Expected a cron expression, e.g. `cron "*/15 * * * *" ping`.')

    if rest[0] in "\"'":
        try:
            parts = shlex.split(rest)
        except ValueError as exc:
            raise RemindParseError(f"Unbalanced quotes: {exc}") from exc
        expression, message_parts = parts[0], parts[1:]
        message = " ".join(message_parts)
    else:
        # Unquoted: the first five whitespace-separated fields are the schedule.
        fields = rest.split(maxsplit=5)
        if len(fields) < 6:
            raise RemindParseError(
                'A cron schedule needs five fields plus a message, e.g. '
                '`cron */15 * * * * ping`.'
            )
        expression, message = " ".join(fields[:5]), fields[5]

    from apscheduler.triggers.cron import CronTrigger  # local: validation only

    try:
        CronTrigger.from_crontab(expression)
    except ValueError as exc:
        raise RemindParseError(f"Invalid cron expression `{expression}`: {exc}") from exc

    return ParsedReminder(
        schedule_kind=ScheduleKind.cron.value,
        cron_expr=expression,
        message=_require_message([message], "reminder"),
        timezone_name=tz_name,
        channel_name=channel,
    )


def _parse_once(
    tokens: list[str], tz_name: str, channel: str | None, tz: ZoneInfo
) -> ParsedReminder:
    if len(tokens) >= 2 and DATE_RE.match(tokens[0]) and TIME_RE.match(tokens[1]):
        date_part, time_part, rest = tokens[0], tokens[1], tokens[2:]
    elif tokens and TIME_RE.match(tokens[0]):
        date_part, time_part, rest = None, tokens[0], tokens[1:]
    else:
        raise RemindParseError(
            "Expected `at YYYY-MM-DD HH:MM <message>` or `at HH:MM <message>`."
        )

    hour, minute = (int(x) for x in normalize_time_of_day(time_part).split(":"))
    now = datetime.now(tz)
    if date_part:
        year, month, day = (int(x) for x in date_part.split("-"))
        try:
            run_at = datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError as exc:
            raise RemindParseError(f"Invalid date {date_part}: {exc}") from exc
    else:
        run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)  # "at 08:00" tomorrow if 08:00 has passed

    if run_at <= now:
        raise RemindParseError(
            f"{run_at.isoformat(timespec='minutes')} is in the past."
        )

    return ParsedReminder(
        schedule_kind=ScheduleKind.once.value,
        run_at=run_at,
        message=_require_message(rest, "reminder"),
        timezone_name=tz_name,
        channel_name=channel,
    )


def _parse_relative(
    tokens: list[str], tz_name: str, channel: str | None, tz: ZoneInfo
) -> ParsedReminder:
    if not tokens:
        raise RemindParseError("Expected a delay like `in 30m take a break`.")

    # Accept both "30m" and "30 minutes".
    delta = _parse_duration(tokens[0])
    rest = tokens[1:]
    if delta is None and len(tokens) >= 2:
        delta = _parse_duration(f"{tokens[0]} {tokens[1]}")
        rest = tokens[2:]
    if delta is None:
        raise RemindParseError(
            "I could not read that delay. Try `in 30m`, `in 2h`, or `in 1 day`."
        )
    if delta <= timedelta(0):
        raise RemindParseError("The delay must be positive.")

    return ParsedReminder(
        schedule_kind=ScheduleKind.once.value,
        run_at=datetime.now(tz) + delta,
        message=_require_message(rest, "reminder"),
        timezone_name=tz_name,
        channel_name=channel,
    )
