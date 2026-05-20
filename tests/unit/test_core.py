"""
Unit tests for Claw.

These tests cover pure functions only — no network calls, no DB, no file I/O.
They should run in milliseconds with no credentials required.

Run with: pytest tests/unit/
"""

import pytest
from datetime import date, datetime, timedelta

from claw.todoist_client import Task


# ─── Fixtures ────────────────────────────────────────────────────────────────

def make_task(
    id="task-001",
    content="Write the quarterly report",
    description="",
    project_id="proj-1",
    project_name="work",
    section_id="sec-today",
    section_name="Today",
    labels=None,
    due_date=None,
    days_overdue=0,
    priority=1,
    is_habit=False,
    is_waiting=False,
) -> Task:
    return Task(
        id=id,
        content=content,
        description=description,
        project_id=project_id,
        project_name=project_name,
        section_id=section_id,
        section_name=section_name,
        labels=labels or [],
        due_date=due_date,
        priority=priority,
        is_overdue=days_overdue > 0,
        days_overdue=days_overdue,
        is_habit=is_habit,
        is_waiting=is_waiting,
    )


# ─── Task model ──────────────────────────────────────────────────────────────

class TestTaskDisplayName:
    def test_short_content_returned_as_is(self):
        task = make_task(content="Buy milk")
        assert task.display_name == "Buy milk"

    def test_long_content_truncated_at_80_chars(self):
        task = make_task(content="A" * 100)
        assert len(task.display_name) == 80

    def test_exactly_80_chars_not_truncated(self):
        task = make_task(content="A" * 80)
        assert len(task.display_name) == 80


class TestTaskOverdue:
    def test_overdue_task_is_flagged(self):
        task = make_task(days_overdue=3)
        assert task.is_overdue is True

    def test_not_overdue_task(self):
        task = make_task(days_overdue=0)
        assert task.is_overdue is False


# ─── Memory context builder ──────────────────────────────────────────────────

class TestBuildContextBlock:
    """Tests for memory.build_context_block — a pure function."""

    def test_returns_string(self):
        from claw.memory import build_context_block
        result = build_context_block(None, [])
        assert isinstance(result, str)

    def test_no_memory_gives_neutral_context(self):
        from claw.memory import build_context_block
        result = build_context_block(None, [])
        assert "no previous" in result.lower() or "never discussed" in result.lower()

    def test_task_memory_included_in_output(self):
        from claw.memory import build_context_block, TaskMemory
        task_memory = TaskMemory(
            task_id="task-001",
            last_probed_at=datetime.now() - timedelta(days=5),
            probe_count=2,
            last_outcome="user_committed",
            notes="User said they'd finish this by end of last week.",
            snoozed_until=None,
        )
        result = build_context_block(task_memory, [])
        assert "5 day" in result or "last week" in result.lower() or "committed" in result.lower()


# ─── Prompt loading ───────────────────────────────────────────────────────────

class TestPromptLoader:
    def test_default_prompt_returns_string(self):
        from claw import prompts
        result = prompts.get_prompt("BRIEFING_SYSTEM", overrides={})
        assert isinstance(result, str)
        assert len(result) > 50

    def test_override_replaces_default(self):
        from claw import prompts
        overrides = {"BRIEFING_SYSTEM": "custom prompt here"}
        result = prompts.get_prompt("BRIEFING_SYSTEM", overrides=overrides)
        assert result == "custom prompt here"

    def test_unknown_prompt_raises_key_error(self):
        from claw import prompts
        with pytest.raises(KeyError):
            prompts.get_prompt("NONEXISTENT_PROMPT", overrides={})


# ─── Briefing task formatter ─────────────────────────────────────────────────

class TestFormatTasksForPrompt:
    def test_empty_list_returns_no_tasks_message(self):
        from claw.briefing import _format_tasks_for_prompt
        result = _format_tasks_for_prompt([], max_tasks=4)
        assert "no tasks" in result.lower() or result == ""

    def test_single_task_included(self):
        from claw.briefing import _format_tasks_for_prompt
        task = make_task(content="Write report", section_name="Today", project_name="work")
        result = _format_tasks_for_prompt([task], max_tasks=4)
        assert "Write report" in result
        assert "Today" in result
        assert "work" in result

    def test_overdue_badge_shown(self):
        from claw.briefing import _format_tasks_for_prompt
        task = make_task(content="Fix bug", days_overdue=3)
        result = _format_tasks_for_prompt([task], max_tasks=4)
        assert "3d overdue" in result
        assert "⚠️" in result

    def test_non_overdue_has_no_badge(self):
        from claw.briefing import _format_tasks_for_prompt
        task = make_task(content="Review PR", days_overdue=0)
        result = _format_tasks_for_prompt([task], max_tasks=4)
        assert "overdue" not in result

    def test_capped_at_max_tasks(self):
        from claw.briefing import _format_tasks_for_prompt
        tasks = [make_task(id=f"t{i}", content=f"Task {i}") for i in range(10)]
        result = _format_tasks_for_prompt(tasks, max_tasks=3)
        assert result.count("Task") == 3


# ─── Probe format functions ──────────────────────────────────────────────────

class TestFormatTaskForPrompt:
    def test_includes_content_and_section(self):
        from claw.probe import _format_task_for_prompt
        task = make_task(content="Fix the thing", section_name="Next 2-3 Days")
        result = _format_task_for_prompt(task)
        assert "Fix the thing" in result
        assert "Next 2-3 Days" in result

    def test_overdue_badge_shown(self):
        from claw.probe import _format_task_for_prompt
        task = make_task(days_overdue=5)
        result = _format_task_for_prompt(task)
        assert "5 day" in result
        assert "Overdue" in result

    def test_no_overdue_line_when_not_overdue(self):
        from claw.probe import _format_task_for_prompt
        task = make_task(days_overdue=0)
        result = _format_task_for_prompt(task)
        assert "Overdue" not in result

    def test_description_included_when_present(self):
        from claw.probe import _format_task_for_prompt
        task = make_task(description="Waiting on Bob")
        result = _format_task_for_prompt(task)
        assert "Waiting on Bob" in result

    def test_habit_shows_lifestyle_type_marker(self):
        from claw.probe import _format_task_for_prompt
        task = make_task(is_habit=True, content="Strength Training")
        result = _format_task_for_prompt(task)
        assert "LIFESTYLE HABIT" in result

    def test_non_habit_has_no_type_marker(self):
        from claw.probe import _format_task_for_prompt
        task = make_task(is_habit=False)
        result = _format_task_for_prompt(task)
        assert "LIFESTYLE HABIT" not in result


class TestFormatTaskMemory:
    def test_none_returns_no_history_message(self):
        from claw.probe import _format_task_memory
        result = _format_task_memory(None)
        assert "no previous" in result.lower()

    def test_populated_memory_includes_outcome(self):
        from claw.probe import _format_task_memory
        from claw.memory import TaskMemory
        from datetime import timezone
        mem = TaskMemory(
            task_id="t1",
            last_probed_at=datetime.now(timezone.utc) - timedelta(days=3),
            probe_count=2,
            last_outcome="user_committed",
            notes="Said they'd do it Thursday.",
            snoozed_until=None,
        )
        result = _format_task_memory(mem)
        assert "user_committed" in result
        assert "3 day" in result


class TestFormatTaskForSelection:
    def test_includes_task_id_and_content(self):
        from claw.probe import _format_task_for_selection
        task = make_task(id="abc123", content="Write report")
        result = _format_task_for_selection(task, None)
        assert "abc123" in result
        assert "Write report" in result

    def test_never_probed_when_no_memory(self):
        from claw.probe import _format_task_for_selection
        task = make_task()
        result = _format_task_for_selection(task, None)
        assert "never probed" in result

    def test_habit_flagged_in_selection(self):
        from claw.probe import _format_task_for_selection
        task = make_task(is_habit=True, content="Get on top of boozing")
        result = _format_task_for_selection(task, None)
        assert "[HABIT]" in result

    def test_snoozed_flagged(self):
        from claw.probe import _format_task_for_selection
        from claw.memory import TaskMemory
        mem = TaskMemory(
            task_id="t1",
            last_probed_at=None,
            probe_count=0,
            last_outcome=None,
            notes="",
            snoozed_until=datetime(2026, 12, 31),
        )
        result = _format_task_for_selection(make_task(), mem)
        assert "SNOOZED" in result


class TestFindSubtask:
    def _make_subtasks(self):
        return [
            make_task(id="sub-1", content="Find Resistance Bands"),
            make_task(id="sub-2", content="Book physio appointment"),
        ]

    def test_exact_match(self):
        from claw.probe import _find_subtask
        subtasks = self._make_subtasks()
        result = _find_subtask("Find Resistance Bands", subtasks)
        assert result is not None
        assert result.id == "sub-1"

    def test_case_insensitive_match(self):
        from claw.probe import _find_subtask
        subtasks = self._make_subtasks()
        result = _find_subtask("find resistance bands", subtasks)
        assert result is not None
        assert result.id == "sub-1"

    def test_partial_match_fallback(self):
        from claw.probe import _find_subtask
        subtasks = self._make_subtasks()
        result = _find_subtask("resistance bands", subtasks)
        assert result is not None
        assert result.id == "sub-1"

    def test_no_match_returns_none(self):
        from claw.probe import _find_subtask
        subtasks = self._make_subtasks()
        assert _find_subtask("something completely different", subtasks) is None

    def test_empty_list_returns_none(self):
        from claw.probe import _find_subtask
        assert _find_subtask("Find Resistance Bands", []) is None


class TestIsConversationClosed:
    def test_short_no_question_is_closed(self):
        from claw.probe import _is_conversation_closed
        assert _is_conversation_closed("Sounds good. Talk soon.") is True

    def test_question_is_not_closed(self):
        from claw.probe import _is_conversation_closed
        assert _is_conversation_closed("What's blocking you?") is False

    def test_long_response_not_closed(self):
        from claw.probe import _is_conversation_closed
        long = "word " * 70
        assert _is_conversation_closed(long) is False


# ─── Config validation ────────────────────────────────────────────────────────

class TestConfigValidation:
    def test_valid_config_passes(self):
        from claw.config import _validate
        config = {
            "telegram": {"allowed_user_id": 123},
            "todoist": {"projects": ["work", "home"]},
            "memory": {"db_path": "data/claw.db"},
            "litellm": {"base_url": "http://localhost:4000"},
            "claude": {"model": "claude-sonnet-4.6", "selection_model": "llama-3.3-70b"},
            "schedule": {
                "timezone": "Europe/London",
                "active_window_start": "07:00",
                "active_window_end": "21:00",
                "briefing_window_end": "10:00",
                "min_minutes_between_sessions": 90,
            },
        }
        _validate(config)  # Should not raise

    def test_missing_required_key_raises_value_error(self):
        from claw.config import _validate
        config = {
            "telegram": {"allowed_user_id": 123},
            # todoist missing
            "memory": {"db_path": "data/claw.db"},
            "litellm": {"base_url": "http://localhost:4000"},
            "claude": {"model": "claude-sonnet-4.6", "selection_model": "llama-3.3-70b"},
        }
        with pytest.raises(ValueError, match="todoist"):
            _validate(config)

    def test_missing_nested_key_raises_value_error(self):
        from claw.config import _validate
        config = {
            "telegram": {"allowed_user_id": 123},
            "todoist": {"projects": ["work"]},
            "memory": {},  # db_path missing
            "litellm": {"base_url": "http://localhost:4000"},
            "claude": {"model": "claude-sonnet-4.6", "selection_model": "llama-3.3-70b"},
        }
        with pytest.raises(ValueError, match="db_path"):
            _validate(config)

    def test_missing_selection_model_raises_value_error(self):
        from claw.config import _validate
        config = {
            "telegram": {"allowed_user_id": 123},
            "todoist": {"projects": ["work"]},
            "memory": {"db_path": "data/claw.db"},
            "litellm": {"base_url": "http://localhost:4000"},
            "claude": {"model": "claude-sonnet-4.6"},  # selection_model missing
        }
        with pytest.raises(ValueError, match="selection_model"):
            _validate(config)

    def test_missing_litellm_base_url_raises_value_error(self):
        from claw.config import _validate
        config = {
            "telegram": {"allowed_user_id": 123},
            "todoist": {"projects": ["work"]},
            "memory": {"db_path": "data/claw.db"},
            # litellm missing
            "claude": {"model": "claude-sonnet-4.6", "selection_model": "llama-3.3-70b"},
        }
        with pytest.raises(ValueError, match="base_url"):
            _validate(config)


# ─── Snooze detection helpers ─────────────────────────────────────────────────

class TestIsSnoozed:
    def test_no_memory_is_not_snoozed(self):
        import tempfile, os
        from claw.memory import MemoryStore
        from claw.probe import _is_snoozed
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = MemoryStore(db_path)
            task = make_task(id="t1")
            now = datetime(2026, 5, 16, 18, 0, 0, tzinfo=__import__("datetime").timezone.utc)
            assert _is_snoozed(task, store, now) is False
        finally:
            os.unlink(db_path)

    def test_future_snooze_is_snoozed(self):
        import tempfile, os
        from datetime import timezone
        from claw.memory import MemoryStore, TaskMemory
        from claw.probe import _is_snoozed
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = MemoryStore(db_path)
            snooze_dt = datetime(2026, 5, 20, 0, 0, 0, tzinfo=timezone.utc)
            store.upsert_task_memory(TaskMemory(
                task_id="t1", last_probed_at=None, probe_count=0,
                last_outcome=None, notes="", snoozed_until=snooze_dt,
            ))
            task = make_task(id="t1")
            now = datetime(2026, 5, 16, 18, 0, 0, tzinfo=timezone.utc)
            assert _is_snoozed(task, store, now) is True
        finally:
            os.unlink(db_path)

    def test_expired_snooze_is_not_snoozed(self):
        import tempfile, os
        from datetime import timezone
        from claw.memory import MemoryStore, TaskMemory
        from claw.probe import _is_snoozed
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = MemoryStore(db_path)
            snooze_dt = datetime(2026, 5, 10, 0, 0, 0, tzinfo=timezone.utc)
            store.upsert_task_memory(TaskMemory(
                task_id="t1", last_probed_at=None, probe_count=0,
                last_outcome=None, notes="", snoozed_until=snooze_dt,
            ))
            task = make_task(id="t1")
            now = datetime(2026, 5, 16, 18, 0, 0, tzinfo=timezone.utc)
            assert _is_snoozed(task, store, now) is False
        finally:
            os.unlink(db_path)


# ─── Listener offset persistence ──────────────────────────────────────────────

class TestListenerOffset:
    def test_returns_none_before_any_set(self):
        import tempfile, os
        from claw.memory import MemoryStore
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = MemoryStore(db_path)
            assert store.get_listener_offset() is None
        finally:
            os.unlink(db_path)

    def test_set_and_get_roundtrip(self):
        import tempfile, os
        from claw.memory import MemoryStore
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = MemoryStore(db_path)
            store.set_listener_offset(12345)
            assert store.get_listener_offset() == 12345
        finally:
            os.unlink(db_path)

    def test_update_overwrites_previous(self):
        import tempfile, os
        from claw.memory import MemoryStore
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = MemoryStore(db_path)
            store.set_listener_offset(100)
            store.set_listener_offset(200)
            assert store.get_listener_offset() == 200
        finally:
            os.unlink(db_path)


# ─── Waiting For field ───────────────────────────────────────────────────────

class TestIsWaiting:
    def test_waiting_task_flagged(self):
        from claw.todoist_client import WAITING_SECTIONS
        section_id = next(iter(WAITING_SECTIONS))
        task = make_task(section_id=section_id, is_waiting=True)
        assert task.is_waiting is True

    def test_regular_task_not_waiting(self):
        task = make_task(section_id="sec-today", is_waiting=False)
        assert task.is_waiting is False

    def test_waiting_sections_set_is_nonempty(self):
        from claw.todoist_client import WAITING_SECTIONS
        assert len(WAITING_SECTIONS) >= 2  # work and home

    def test_waiting_tag_in_selection_format(self):
        from claw.probe import _format_task_for_selection
        task = make_task(is_waiting=True, content="Invoice from supplier")
        result = _format_task_for_selection(task, None)
        assert "[WAITING]" in result

    def test_waiting_type_in_prompt_format(self):
        from claw.probe import _format_task_for_prompt
        task = make_task(is_waiting=True, content="Invoice from supplier")
        result = _format_task_for_prompt(task)
        assert "WAITING FOR" in result

    def test_habit_tag_not_waiting_tag(self):
        from claw.probe import _format_task_for_selection
        task = make_task(is_habit=True, is_waiting=False, content="Strength Training")
        result = _format_task_for_selection(task, None)
        assert "[HABIT]" in result
        assert "[WAITING]" not in result


# ─── Waiting For briefing formatter ──────────────────────────────────────────

class TestFormatWaitingForPrompt:
    def test_empty_returns_nothing_waiting(self):
        from claw.briefing import _format_waiting_for_prompt
        assert "Nothing waiting" in _format_waiting_for_prompt([])

    def test_single_item_shows_name(self):
        from claw.briefing import _format_waiting_for_prompt
        task = make_task(content="Invoice from supplier", is_waiting=True)
        result = _format_waiting_for_prompt([task])
        assert "Invoice from supplier" in result
        assert "1 item" in result

    def test_multiple_items_shows_count(self):
        from claw.briefing import _format_waiting_for_prompt
        tasks = [
            make_task(id="w1", content="A", is_waiting=True, days_overdue=5),
            make_task(id="w2", content="B", is_waiting=True, days_overdue=2),
        ]
        result = _format_waiting_for_prompt(tasks)
        assert "2 items" in result

    def test_oldest_shown_when_overdue(self):
        from claw.briefing import _format_waiting_for_prompt
        tasks = [
            make_task(id="w1", content="OldOne", is_waiting=True, days_overdue=10),
            make_task(id="w2", content="NewOne", is_waiting=True, days_overdue=1),
        ]
        result = _format_waiting_for_prompt(tasks)
        assert "OldOne" in result
        assert "10d" in result


# ─── Goal parsing ─────────────────────────────────────────────────────────────

class TestParseGoalDescription:
    def test_full_template(self):
        from claw.goals import parse_goal_description
        desc = "Why: Feel confident\nTarget: 85kg\nCurrent: 108kg\nBy: 2026-12-01\nStatus: Making progress"
        result = parse_goal_description(desc)
        assert result["why"] == "Feel confident"
        assert result["target"] == "85kg"
        assert result["current"] == "108kg"
        assert result["by"].isoformat() == "2026-12-01"
        assert result["status"] == "Making progress"

    def test_missing_fields_return_defaults(self):
        from claw.goals import parse_goal_description
        result = parse_goal_description("Why: Feel better")
        assert result["why"] == "Feel better"
        assert result["target"] is None
        assert result["current"] is None
        assert result["by"] is None
        assert result["status"] == ""

    def test_empty_description(self):
        from claw.goals import parse_goal_description
        result = parse_goal_description("")
        assert result["why"] == ""
        assert result["target"] is None

    def test_case_insensitive_keys(self):
        from claw.goals import parse_goal_description
        desc = "WHY: Confidence\ntarget: 100cm\nCURRENT: 112cm"
        result = parse_goal_description(desc)
        assert result["why"] == "Confidence"
        assert result["target"] == "100cm"
        assert result["current"] == "112cm"

    def test_invalid_date_ignored(self):
        from claw.goals import parse_goal_description
        result = parse_goal_description("By: not-a-date")
        assert result["by"] is None

    def test_lines_without_colon_ignored(self):
        from claw.goals import parse_goal_description
        desc = "Some freeform line\nWhy: Feel better\nAnother line"
        result = parse_goal_description(desc)
        assert result["why"] == "Feel better"


class TestGoalForTask:
    def test_matching_label_returns_goal(self):
        from claw.goals import goal_for_task, GoalRecord
        from datetime import date
        goal = GoalRecord(task_id="g1", name="Weight to 85kg", labels=["health"],
                         why="Feel better", target="85kg", current="108kg",
                         by=None, status="")
        task = make_task(labels=["health"])
        assert goal_for_task(task, [goal]) is goal

    def test_no_matching_label_returns_none(self):
        from claw.goals import goal_for_task, GoalRecord
        goal = GoalRecord(task_id="g1", name="Weight to 85kg", labels=["health"],
                         why="", target="85kg", current=None, by=None, status="")
        task = make_task(labels=["work", "alpha"])
        assert goal_for_task(task, [goal]) is None

    def test_first_matching_goal_wins(self):
        from claw.goals import goal_for_task, GoalRecord
        goal1 = GoalRecord(task_id="g1", name="Goal A", labels=["health"],
                          why="", target=None, current=None, by=None, status="")
        goal2 = GoalRecord(task_id="g2", name="Goal B", labels=["health"],
                          why="", target=None, current=None, by=None, status="")
        task = make_task(labels=["health"])
        assert goal_for_task(task, [goal1, goal2]) is goal1

    def test_empty_goals_returns_none(self):
        from claw.goals import goal_for_task
        task = make_task(labels=["health"])
        assert goal_for_task(task, []) is None


class TestGoalLineForTask:
    def test_full_goal_renders_all_fields(self):
        from claw.goals import goal_line_for_task, GoalRecord
        from datetime import date
        goal = GoalRecord(task_id="g1", name="Weight to 85kg", labels=["health"],
                         why="Feel confident", target="85kg", current="108kg",
                         by=None, status="Improving")
        task = make_task(labels=["health"])
        line = goal_line_for_task(task, [goal])
        assert "Weight to 85kg" in line
        assert "108kg" in line
        assert "85kg" in line
        assert "Feel confident" in line
        assert "Improving" in line

    def test_no_goal_returns_empty_string(self):
        from claw.goals import goal_line_for_task
        task = make_task(labels=[])
        assert goal_line_for_task(task, []) == ""

    def test_goal_without_current_shows_target_only(self):
        from claw.goals import goal_line_for_task, GoalRecord
        goal = GoalRecord(task_id="g1", name="Waist to 100cm", labels=["health"],
                         why="", target="100cm", current=None, by=None, status="")
        task = make_task(labels=["health"])
        line = goal_line_for_task(task, [goal])
        assert "100cm" in line
        assert "→" not in line


# ─── Description field update ─────────────────────────────────────────────────

class TestUpdateDescriptionField:
    def test_replaces_existing_field(self):
        from claw.todoist_client import _update_description_field
        desc = "Why: Feel better\nCurrent: 108kg\nTarget: 85kg"
        result = _update_description_field(desc, "Current", "107kg")
        assert "Current: 107kg" in result
        assert "108kg" not in result

    def test_appends_missing_field(self):
        from claw.todoist_client import _update_description_field
        desc = "Why: Feel better\nTarget: 85kg"
        result = _update_description_field(desc, "Current", "107kg")
        assert "Current: 107kg" in result
        assert "Why: Feel better" in result

    def test_empty_description_creates_field(self):
        from claw.todoist_client import _update_description_field
        result = _update_description_field("", "Current", "107kg")
        assert result == "Current: 107kg"

    def test_case_insensitive_key_match(self):
        from claw.todoist_client import _update_description_field
        desc = "current: 108kg\nTarget: 85kg"
        result = _update_description_field(desc, "Current", "107kg")
        assert "Current: 107kg" in result
        assert "108kg" not in result
