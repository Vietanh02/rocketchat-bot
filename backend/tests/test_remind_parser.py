"""The chat syntax for creating reminders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bot.remind_parser import RemindParseError, parse_reminder


def test_bare_time_means_daily():
    parsed = parse_reminder("09:00 stand-up notes")
    assert parsed.schedule_kind == "daily"
    assert parsed.time_of_day == "09:00"
    assert parsed.message == "stand-up notes"


@pytest.mark.parametrize("text", ["daily 9:5 x", "9:5 x", "weekly mon 9:5 x"])
def test_times_are_normalised(text):
    assert parse_reminder(text).time_of_day == "09:05"


def test_a_bare_number_is_not_a_time():
    """"100 pushups" must not be read as 1:00."""
    with pytest.raises(RemindParseError):
        parse_reminder("100 pushups daily")


@pytest.mark.parametrize(
    "text",
    ["weekly mon,fri 09:00 report", "mon, fri 09:00 report", "monday,friday 09:00 report"],
)
def test_weekly_forms(text):
    parsed = parse_reminder(text)
    assert parsed.schedule_kind == "weekly"
    assert parsed.days_of_week == "mon,fri"
    assert parsed.message == "report"


def test_a_message_starting_with_a_weekdayish_word_is_not_a_schedule():
    """"monitor" starts with "mon" but must not be read as Monday."""
    with pytest.raises(RemindParseError) as excinfo:
        parse_reminder("monitor the queue")
    assert "could not find a time" in str(excinfo.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [('cron "*/15 9-17 * * 1-5" check', "*/15 9-17 * * 1-5"), ("cron */15 * * * * ping", "*/15 * * * *")],
)
def test_cron_quoted_and_unquoted(text, expected):
    parsed = parse_reminder(text)
    assert parsed.schedule_kind == "cron"
    assert parsed.cron_expr == expected


def test_relative_delay():
    before = datetime.now(timezone.utc)
    parsed = parse_reminder("in 30m take a break")
    assert parsed.schedule_kind == "once"
    delta = parsed.run_at - before
    assert timedelta(minutes=29) < delta < timedelta(minutes=31)
    assert parsed.message == "take a break"


def test_relative_delay_with_spelled_unit():
    parsed = parse_reminder("in 2 hours stretch")
    assert parsed.run_at - datetime.now(timezone.utc) > timedelta(minutes=110)


def test_bare_time_in_the_past_rolls_to_tomorrow():
    parsed = parse_reminder("at 00:01 midnight-ish")
    assert parsed.run_at > datetime.now(parsed.run_at.tzinfo)


def test_timezone_and_channel_modifiers_are_extracted():
    parsed = parse_reminder("daily 07:30 tz=Asia/Ho_Chi_Minh morning sync #general")
    assert parsed.timezone_name == "Asia/Ho_Chi_Minh"
    assert parsed.channel_name == "general"
    assert parsed.message == "morning sync"
    assert parsed.time_of_day == "07:30"


def test_default_timezone_is_applied():
    assert parse_reminder("09:00 x", default_timezone="Europe/Berlin").timezone_name == "Europe/Berlin"


def test_title_is_derived_from_the_first_line():
    assert parse_reminder("09:00 first line").title == "first line"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "when and what"),
        ("tomorrow sometime", "could not find a time"),
        ("daily 99:00 x", "Expected a time"),
        ("daily 09:00", "What should I say"),
        ("in 0m x", "must be positive"),
        ("at 2020-01-01 09:00 past", "in the past"),
        ("weekly funday 09:00 x", "unknown day of week"),
        ("cron bad x", "five fields"),
        ("cron \"not cron\" x", "Invalid cron"),
        ("daily 09:00 x tz=Mars/Base", "unknown timezone"),
    ],
)
def test_parse_errors_explain_themselves(text, fragment):
    with pytest.raises(RemindParseError) as excinfo:
        parse_reminder(text)
    assert fragment in str(excinfo.value)
