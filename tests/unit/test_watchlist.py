"""
Unit tests for watchlist overdue-topic detection (B2).

Uses a real temp-file MemoryStore to seed last_probed_at values (the module's
:memory: design opens a fresh connection per call, so a file DB is required).

Run with: pytest tests/unit/
"""

from datetime import date, datetime, timedelta, timezone

from claw.memory import MemoryStore, TaskMemory
from claw.todoist_client import Task
from claw.watchlist import get_overdue_topics


def _habit(task_id, content="Stretch") -> Task:
    return Task(
        id=task_id, content=content, description="",
        project_id="p", project_name="claw",
        section_id="s", section_name="Life Style",
        labels=[], due_date=None, priority=1,
        is_overdue=False, days_overdue=0,
        is_habit=True, is_waiting=False,
    )


def _seed_probe(memory, task_id, days_ago):
    probed = datetime.now(timezone.utc) - timedelta(days=days_ago)
    memory.upsert_task_memory(TaskMemory(
        task_id=task_id, last_probed_at=probed, probe_count=1,
        last_outcome=None, notes="", snoozed_until=None,
    ))


def _config(threshold=7):
    return {"watchlist": {"silence_threshold_days": threshold}}


class TestGetOverdueTopics:
    def test_recently_probed_habit_not_overdue(self, tmp_path):
        memory = MemoryStore(str(tmp_path / "c.db"))
        habit = _habit("h1")
        _seed_probe(memory, "h1", days_ago=2)

        topics = get_overdue_topics(memory, [habit], [], None, _config(), date.today())
        assert topics == []

    def test_silent_habit_is_overdue(self, tmp_path):
        memory = MemoryStore(str(tmp_path / "c.db"))
        habit = _habit("h1")
        _seed_probe(memory, "h1", days_ago=10)

        topics = get_overdue_topics(memory, [habit], [], None, _config(), date.today())
        assert len(topics) == 1
        assert topics[0].topic_type == "habit"
        assert topics[0].days_silent == 10

    def test_never_probed_habit_is_overdue(self, tmp_path):
        memory = MemoryStore(str(tmp_path / "c.db"))
        habit = _habit("h1")
        # no memory row seeded → treated as maximally silent

        topics = get_overdue_topics(memory, [habit], [], None, _config(), date.today())
        assert len(topics) == 1
        assert topics[0].days_silent == 9999

    def test_sorted_most_silent_first(self, tmp_path):
        memory = MemoryStore(str(tmp_path / "c.db"))
        h1, h2 = _habit("h1", "Floss"), _habit("h2", "Walk")
        _seed_probe(memory, "h1", days_ago=8)
        _seed_probe(memory, "h2", days_ago=20)

        topics = get_overdue_topics(memory, [h1, h2], [], None, _config(), date.today())
        assert [t.task.id for t in topics] == ["h2", "h1"]

    def test_non_positive_threshold_disables_watchlist(self, tmp_path):
        memory = MemoryStore(str(tmp_path / "c.db"))
        habit = _habit("h1")
        _seed_probe(memory, "h1", days_ago=100)

        topics = get_overdue_topics(memory, [habit], [], None, _config(threshold=0), date.today())
        assert topics == []

    def test_non_habit_task_ignored(self, tmp_path):
        memory = MemoryStore(str(tmp_path / "c.db"))
        regular = _habit("r1")
        object.__setattr__(regular, "is_habit", False)
        _seed_probe(memory, "r1", days_ago=30)

        topics = get_overdue_topics(memory, [regular], [], None, _config(), date.today())
        assert topics == []
