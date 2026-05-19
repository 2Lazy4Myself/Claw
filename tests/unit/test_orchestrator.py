"""
Unit tests for orchestrator decision functions.
All tests use fixed datetimes — no network, no DB.
"""

from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo
import pytest

from claw.orchestrator import (
    _within_active_window,
    _briefing_window_open,
    _parse_hhmm,
)


LONDON = ZoneInfo("Europe/London")

CONFIG = {
    "schedule": {
        "timezone": "Europe/London",
        "active_window_start": "07:00",
        "active_window_end": "21:00",
        "briefing_window_end": "10:00",
        "min_minutes_between_sessions": 90,
    }
}


def _local(hour: int, minute: int = 0) -> datetime:
    """Returns a timezone-aware datetime in Europe/London at the given time."""
    return datetime(2026, 5, 19, hour, minute, 0, tzinfo=LONDON)


# ─── _within_active_window ────────────────────────────────────────────────────

class TestWithinActiveWindow:
    def test_before_window(self):
        assert not _within_active_window(CONFIG, _local(6, 59))

    def test_at_window_start(self):
        assert _within_active_window(CONFIG, _local(7, 0))

    def test_mid_window(self):
        assert _within_active_window(CONFIG, _local(14, 0))

    def test_at_window_end(self):
        # 21:00 is excluded (< not <=)
        assert not _within_active_window(CONFIG, _local(21, 0))

    def test_after_window(self):
        assert not _within_active_window(CONFIG, _local(22, 30))

    def test_just_before_end(self):
        assert _within_active_window(CONFIG, _local(20, 59))


# ─── _briefing_window_open ────────────────────────────────────────────────────

class TestBriefingWindowOpen:
    def test_in_briefing_window(self):
        assert _briefing_window_open(CONFIG, _local(7, 30))

    def test_at_briefing_window_start(self):
        assert _briefing_window_open(CONFIG, _local(7, 0))

    def test_at_briefing_window_end(self):
        # 10:00 is excluded
        assert not _briefing_window_open(CONFIG, _local(10, 0))

    def test_after_briefing_window(self):
        assert not _briefing_window_open(CONFIG, _local(11, 0))

    def test_evening_not_briefing_window(self):
        assert not _briefing_window_open(CONFIG, _local(18, 0))

    def test_just_before_briefing_end(self):
        assert _briefing_window_open(CONFIG, _local(9, 59))


# ─── _parse_hhmm ─────────────────────────────────────────────────────────────

class TestParseHHMM:
    def test_midnight(self):
        assert _parse_hhmm("00:00") == time(0, 0)

    def test_morning(self):
        assert _parse_hhmm("07:00") == time(7, 0)

    def test_afternoon(self):
        assert _parse_hhmm("18:30") == time(18, 30)

    def test_end_of_day(self):
        assert _parse_hhmm("21:00") == time(21, 0)
