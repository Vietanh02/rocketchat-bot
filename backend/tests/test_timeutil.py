"""The UTC storage boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.timeutil import format_local, in_timezone, read_utc, to_utc

HANOI = ZoneInfo("Asia/Ho_Chi_Minh")


def test_aware_values_are_converted_to_utc():
    stored = to_utc(datetime(2026, 10, 20, 9, 0, tzinfo=HANOI))
    assert stored == datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)


def test_naive_values_are_assumed_to_be_utc():
    assert to_utc(datetime(2026, 10, 20, 2, 0)).tzinfo == timezone.utc


def test_reading_back_a_stripped_value_restores_utc():
    """SQLite returns naive datetimes; they must be read as UTC, not local."""
    stored = to_utc(datetime(2026, 10, 20, 9, 0, tzinfo=HANOI))
    assert read_utc(stored.replace(tzinfo=None)) == stored


def test_display_converts_to_the_named_timezone():
    stored = to_utc(datetime(2026, 10, 20, 9, 0, tzinfo=HANOI)).replace(tzinfo=None)
    assert format_local(stored, "Asia/Ho_Chi_Minh") == "2026-10-20 09:00 +07"


def test_an_unknown_timezone_falls_back_to_utc_rather_than_failing():
    stored = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)
    assert "UTC" in format_local(stored, "Mars/Base")


def test_none_is_handled_everywhere():
    assert to_utc(None) is None
    assert read_utc(None) is None
    assert in_timezone(None, "UTC") is None
    assert format_local(None, "UTC") == "not scheduled"


def test_a_round_trip_preserves_the_instant():
    original = datetime(2026, 3, 29, 2, 30, tzinfo=ZoneInfo("Europe/London"))
    assert in_timezone(to_utc(original).replace(tzinfo=None), "Europe/London") == original
