"""
Unit tests for the weekly review ritual (F3).

Covers the orchestrator cadence gate (_weekly_review_due) and the review run
(context assembly → send → session log), with I/O mocked.

Run with: pytest tests/unit/
"""

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from claw import weekly
from claw.orchestrator import _weekly_review_due

LONDON = ZoneInfo("Europe/London")

CONFIG = {
    "schedule": {
        "timezone": "Europe/London",
        "active_window_start": "07:00",
        "active_window_end": "21:00",
        "briefing_window_end": "10:00",
        "nightly_synthesis_after": "20:00",
        "min_minutes_between_sessions": 90,
        "weekly_review_day": 6,  # Sunday
    },
    "claude": {"probe_max_tokens": 2000},
}

# 2026-06-14 is a Sunday; 2026-06-15 a Monday.
SUNDAY_2030 = datetime(2026, 6, 14, 20, 30, tzinfo=LONDON)
SUNDAY_1900 = datetime(2026, 6, 14, 19, 0, tzinfo=LONDON)
MONDAY_2030 = datetime(2026, 6, 15, 20, 30, tzinfo=LONDON)


def _memory(last_weekly=None):
    m = MagicMock()
    m.get_last_weekly_date.return_value = last_weekly
    return m


class TestWeeklyReviewDue:
    def test_due_on_review_day_in_window_never_run(self):
        assert _weekly_review_due(CONFIG, SUNDAY_2030, _memory(None)) is True

    def test_not_due_on_wrong_day(self):
        assert _weekly_review_due(CONFIG, MONDAY_2030, _memory(None)) is False

    def test_not_due_before_window(self):
        assert _weekly_review_due(CONFIG, SUNDAY_1900, _memory(None)) is False

    def test_not_due_if_already_run_this_week(self):
        assert _weekly_review_due(CONFIG, SUNDAY_2030, _memory("2026-06-14")) is False

    def test_due_if_last_run_was_a_previous_week(self):
        assert _weekly_review_due(CONFIG, SUNDAY_2030, _memory("2026-06-07")) is True


class TestRunWeeklyReview:
    def test_sends_reflection_and_logs_session(self):
        todoist = MagicMock()
        todoist.get_claw_data.return_value = ([], [])  # no habits, no goals
        memory = MagicMock()
        memory.get_sessions_since_days.return_value = []
        telegram = MagicMock()
        claude = MagicMock()
        claude.complete.return_value = "Here's how your week went."

        weekly.run_weekly_review(todoist, memory, claude, telegram, CONFIG)

        telegram.send_message.assert_called_once_with("Here's how your week went.")
        logged = memory.log_session.call_args.args[0]
        assert logged.session_type == "weekly"

    def test_failure_escalates_to_error_channel(self):
        todoist = MagicMock()
        todoist.get_claw_data.side_effect = RuntimeError("todoist down")
        telegram = MagicMock()

        weekly.run_weekly_review(todoist, MagicMock(), MagicMock(), telegram, CONFIG)

        telegram.send_error.assert_called_once()
        assert "Weekly review failed" in telegram.send_error.call_args.args[0]
