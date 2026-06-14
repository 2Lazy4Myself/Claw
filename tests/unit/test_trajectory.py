"""
Unit tests for goal trajectory tracking (F1).

Pure trend math + the measurement store roundtrip.

Run with: pytest tests/unit/
"""

from datetime import date, datetime, timedelta, timezone

from claw.memory import MemoryStore
from claw.trajectory import Measurement, parse_measurement, to_measurement, trajectory_note


def _m(numeric, days_ago, value=None):
    when = datetime(2026, 6, 1, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return Measurement(value=value or f"{numeric}kg", numeric=float(numeric), recorded_at=when)


class TestParseMeasurement:
    def test_unit_suffix(self):
        assert parse_measurement("108kg") == 108.0

    def test_space_and_unit(self):
        assert parse_measurement("85 kg") == 85.0

    def test_decimal_and_percent(self):
        assert parse_measurement("12.5%") == 12.5

    def test_thousands_comma(self):
        assert parse_measurement("1,200") == 1200.0

    def test_no_number(self):
        assert parse_measurement("lots") is None
        assert parse_measurement(None) is None


class TestTrajectoryNote:
    TODAY = date(2026, 6, 1)

    def test_needs_two_points(self):
        assert trajectory_note("85kg", None, [_m(100, 0)], self.TODAY) == ""

    def test_needs_numeric_target(self):
        assert trajectory_note(None, None, [_m(100, 10), _m(98, 0)], self.TODAY) == ""

    def test_on_pace_ahead_of_deadline(self):
        # 108 → 102 over 30 days = -0.2/day; 17 to go → ~85 days out.
        pts = [_m(108, 30), _m(102, 0)]
        note = trajectory_note("85kg", date(2027, 6, 1), pts, self.TODAY)
        assert "Trend: 108kg → 102kg over 30d" in note
        assert "-0.20/day" in note
        assert "On pace for 85kg" in note
        assert "ahead of" in note

    def test_behind_deadline(self):
        # slow loss, deadline very soon → behind
        pts = [_m(108, 30), _m(107, 0)]
        note = trajectory_note("85kg", date(2026, 6, 20), pts, self.TODAY)
        assert "behind" in note

    def test_moving_away(self):
        # weight rising (80 → 82) while the target (75) is below → moving away
        pts = [_m(80, 14), _m(82, 0)]
        assert "moving away" in trajectory_note("75kg", None, pts, self.TODAY)

    def test_no_movement(self):
        pts = [_m(100, 10), _m(100, 0)]
        note = trajectory_note("85kg", None, pts, self.TODAY)
        assert "no movement" in note

    def test_zero_span_returns_empty(self):
        same = _m(100, 0)
        assert trajectory_note("85kg", None, [same, _m(98, 0)], self.TODAY) == ""


class TestMeasurementStore:
    def test_roundtrip_oldest_first(self, tmp_path):
        mem = MemoryStore(str(tmp_path / "c.db"))
        t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
        mem.add_goal_measurement("g1", "108kg", 108.0, t0)
        mem.add_goal_measurement("g1", "102kg", 102.0, t0 + timedelta(days=20))
        mem.add_goal_measurement("g2", "5km", 5.0, t0)

        rows = mem.get_goal_measurements("g1")
        assert [r["numeric"] for r in rows] == [108.0, 102.0]
        assert rows[0]["value"] == "108kg"
        assert mem.get_goal_measurements("unknown") == []

    def test_non_numeric_stored_as_null(self, tmp_path):
        mem = MemoryStore(str(tmp_path / "c.db"))
        mem.add_goal_measurement("g1", "felt strong", None)
        rows = mem.get_goal_measurements("g1")
        assert rows[0]["numeric"] is None
