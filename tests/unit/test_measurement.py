"""
Unit tests for spontaneous measurement capture (general-chat path).

A measurement stated out of the blue ("my waist is 109cm") updates the matching
goal and records a trajectory point, regardless of probe/watchlist state.

Run with: pytest tests/unit/
"""

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

from claw import listener
from claw.goals import GoalRecord
from claw.memory import MemoryStore


def _goal(name="Waist", target="85cm", current="110cm", by=None, task_id="g-waist"):
    return GoalRecord(
        task_id=task_id, name=name, labels=["waist"], why="health",
        target=target, current=current, by=by, status="",
    )


def _config():
    return {"claude": {"selection_model": "sel"}}


def _claude(obj):
    c = MagicMock()
    c.complete.return_value = json.dumps(obj)
    return c


class TestMeasurementCapture:
    def test_no_measurable_goals_returns_false_without_calling_model(self):
        claude = MagicMock()
        handled = listener._handle_measurement_capture(
            "my waist is 109cm", [], MagicMock(), MagicMock(), claude, MagicMock(), _config()
        )
        assert handled is False
        claude.complete.assert_not_called()

    def test_match_updates_goal_and_records_point(self, tmp_path):
        memory = MemoryStore(str(tmp_path / "c.db"))
        todoist = MagicMock()
        telegram = MagicMock()
        claude = _claude({"matched": True, "goal": "Waist", "value": "109cm"})

        handled = listener._handle_measurement_capture(
            "my waist is 109cm", [_goal()], todoist, memory, claude, telegram, _config()
        )

        assert handled is True
        todoist.update_goal_current.assert_called_once_with("g-waist", "109cm")
        rows = memory.get_goal_measurements("g-waist")
        assert rows[-1]["value"] == "109cm" and rows[-1]["numeric"] == 109.0
        assert "Waist now 109cm" in telegram.send_message.call_args.args[0]

    def test_no_match_falls_through(self):
        todoist = MagicMock()
        claude = _claude({"matched": False})
        handled = listener._handle_measurement_capture(
            "how's it going?", [_goal()], todoist, MagicMock(), claude, MagicMock(), _config()
        )
        assert handled is False
        todoist.update_goal_current.assert_not_called()

    def test_unknown_goal_name_falls_through(self):
        todoist = MagicMock()
        claude = _claude({"matched": True, "goal": "Bench press", "value": "100kg"})
        handled = listener._handle_measurement_capture(
            "bench is 100kg", [_goal()], todoist, MagicMock(), claude, MagicMock(), _config()
        )
        assert handled is False
        todoist.update_goal_current.assert_not_called()

    def test_trajectory_note_appended_with_history(self, tmp_path):
        memory = MemoryStore(str(tmp_path / "c.db"))
        # seed an earlier reading 30 days ago so the new one yields a trend line
        memory.add_goal_measurement(
            "g-waist", "115cm", 115.0,
            datetime.now(timezone.utc) - timedelta(days=30),
        )
        todoist = MagicMock()
        telegram = MagicMock()
        claude = _claude({"matched": True, "goal": "Waist", "value": "109cm"})

        listener._handle_measurement_capture(
            "waist 109cm now", [_goal(by=date(2027, 1, 1))], todoist, memory, claude, telegram, _config()
        )

        sent = telegram.send_message.call_args.args[0]
        assert "Trend:" in sent  # second data point produced a trajectory line
