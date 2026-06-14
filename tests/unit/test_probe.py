"""
Unit tests for the probe orchestration flow.

These cover the control flow of _run_probe_inner with mocked dependencies —
no network, no DB, no Claude calls.

Run with: pytest tests/unit/
"""

from unittest.mock import MagicMock

from claw.todoist_client import Task


def _make_task(task_id="t-1", content="Do the thing") -> Task:
    return Task(
        id=task_id,
        content=content,
        description="",
        project_id="proj-1",
        project_name="work",
        section_id="sec-today",
        section_name="Today",
        labels=[],
        due_date=None,
        priority=1,
        is_overdue=False,
        days_overdue=0,
        is_habit=False,
        is_waiting=False,
    )


class TestWatchlistCheckinPath:
    """Regression guard for the user_profile UnboundLocalError (probe.py).

    The watchlist check-in branch runs before the constant-cleaning loop and
    passes user_profile to _run_checkin. user_profile must be resolved *before*
    the watchlist branch, otherwise this path raises UnboundLocalError on every
    overdue check-in.
    """

    def test_checkin_receives_user_profile(self, monkeypatch):
        from claw import probe

        todoist = MagicMock()
        todoist.get_today_and_overdue.return_value = [_make_task()]
        todoist.get_claw_data.return_value = ([], [])
        todoist.get_waiting_for.return_value = []
        todoist.get_programmes.return_value = []

        memory = MagicMock()
        memory.pending_count.return_value = 0
        memory.get_user_profile.return_value = "Jake responds well to dry, concise nudges."

        # Force the watchlist branch to fire with one overdue topic.
        overdue_topic = MagicMock(topic_name="fitness programme", days_silent=12)
        monkeypatch.setattr(probe, "get_overdue_topics", lambda *a, **k: [overdue_topic])

        # Capture the check-in call instead of running a real probe conversation.
        run_checkin = MagicMock()
        monkeypatch.setattr(probe, "_run_checkin", run_checkin)

        config = {
            "schedule": {"max_pending_messages": 3},
            "todoist": {"projects": ["work"]},
        }

        # Before the fix this raised UnboundLocalError before reaching _run_checkin.
        probe._run_probe_inner(todoist, memory, MagicMock(), MagicMock(), config)

        run_checkin.assert_called_once()
        assert run_checkin.call_args.kwargs["user_profile"] == (
            "Jake responds well to dry, concise nudges."
        )
