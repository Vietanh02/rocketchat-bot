"""Schedule parsing and next-run arithmetic, including across timezones."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.models import ScheduledTask
from app.scheduler.triggers import (
    ScheduleError,
    describe,
    next_fire_time,
    normalize_days,
    normalize_time_of_day,
)


def task(**kwargs) -> ScheduledTask:
    kwargs.setdefault("title", "t")
    return ScheduledTask(**kwargs)


def test_daily_next_run_respects_the_task_timezone():
    t = task(schedule_kind="daily", time_of_day="09:00", timezone_name="Asia/Ho_Chi_Minh")
    after = datetime(2026, 10, 19, 12, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    nxt = next_fire_time(t, after=after)
    assert nxt.hour == 9 and nxt.day == 20
    assert nxt.utcoffset().total_seconds() == 7 * 3600


def test_the_same_wall_clock_time_is_a_different_instant_per_timezone():
    after_utc = datetime(2026, 10, 19, 12, 0, tzinfo=ZoneInfo("UTC"))
    hanoi = next_fire_time(
        task(schedule_kind="daily", time_of_day="09:00", timezone_name="Asia/Ho_Chi_Minh"),
        after=after_utc.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")),
    )
    london = next_fire_time(
        task(schedule_kind="daily", time_of_day="09:00", timezone_name="Europe/London"),
        after=after_utc.astimezone(ZoneInfo("Europe/London")),
    )
    assert hanoi.astimezone(ZoneInfo("UTC")) != london.astimezone(ZoneInfo("UTC"))


def test_weekly_picks_the_next_listed_day():
    t = task(
        schedule_kind="weekly", time_of_day="09:30", days_of_week="mon,wed", timezone_name="UTC"
    )
    # 2026-10-19 is a Monday; 12:00 is past 09:30, so Wednesday is next.
    nxt = next_fire_time(t, after=datetime(2026, 10, 19, 12, 0, tzinfo=ZoneInfo("UTC")))
    assert nxt.strftime("%a") == "Wed"


def test_cron_expression():
    t = task(schedule_kind="cron", cron_expr="*/15 * * * *", timezone_name="UTC")
    nxt = next_fire_time(t, after=datetime(2026, 10, 19, 12, 1, tzinfo=ZoneInfo("UTC")))
    assert (nxt.hour, nxt.minute) == (12, 15)


def test_once_run_at_is_read_as_utc_when_naive():
    """SQLite strips offsets, so a naive run_at must be treated as UTC."""
    t = task(
        schedule_kind="once",
        run_at=datetime(2026, 10, 19, 14, 41),
        timezone_name="Asia/Ho_Chi_Minh",
    )
    assert "21:41" in describe(t)


def test_dst_spring_forward_does_not_crash():
    t = task(schedule_kind="daily", time_of_day="02:30", timezone_name="Europe/London")
    assert next_fire_time(t, after=datetime(2027, 3, 27, 12, 0, tzinfo=ZoneInfo("Europe/London")))


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Monday, wed", "mon,wed"), ("SUN", "sun"), ("fri,mon", "mon,fri"), (None, "mon,tue,wed,thu,fri,sat,sun")],
)
def test_normalize_days(value, expected):
    assert normalize_days(value) == expected


def test_normalize_time_of_day_accepts_compact_form():
    assert normalize_time_of_day("0905") == "09:05"


@pytest.mark.parametrize(
    "t",
    [
        task(schedule_kind="daily", time_of_day="99:00"),
        task(schedule_kind="daily", time_of_day=None),
        task(schedule_kind="weekly", time_of_day="09:00", days_of_week="funday"),
        task(schedule_kind="cron", cron_expr="nope"),
        task(schedule_kind="cron", cron_expr=None),
        task(schedule_kind="once", run_at=None),
        task(schedule_kind="daily", time_of_day="09:00", timezone_name="Mars/Base"),
        task(schedule_kind="unknown"),
    ],
)
def test_bad_schedules_raise_schedule_error(t):
    with pytest.raises(ScheduleError):
        next_fire_time(t)
