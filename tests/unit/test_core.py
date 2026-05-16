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
    project_name="Work",
    labels=None,
    due_date=None,
    days_overdue=0,
    priority=1,
) -> Task:
    return Task(
        id=id,
        content=content,
        description=description,
        project_id=project_id,
        project_name=project_name,
        labels=labels or [],
        due_date=due_date or date.today(),
        created_at=datetime.now() - timedelta(days=10),
        priority=priority,
        is_overdue=days_overdue > 0,
        days_overdue=days_overdue,
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


# ─── Config validation ────────────────────────────────────────────────────────

class TestConfigValidation:
    def test_valid_config_passes(self):
        from claw.config import _validate
        config = {
            "telegram": {"allowed_user_id": 123},
            "todoist": {},
            "memory": {"db_path": "data/claw.db"},
            "claude": {"model": "claude-sonnet-4-20250514"},
        }
        _validate(config)  # Should not raise

    def test_missing_required_key_raises_value_error(self):
        from claw.config import _validate
        config = {
            "telegram": {"allowed_user_id": 123},
            # todoist missing
            "memory": {"db_path": "data/claw.db"},
            "claude": {"model": "claude-sonnet-4-20250514"},
        }
        with pytest.raises(ValueError, match="todoist"):
            _validate(config)

    def test_missing_nested_key_raises_value_error(self):
        from claw.config import _validate
        config = {
            "telegram": {"allowed_user_id": 123},
            "todoist": {},
            "memory": {},  # db_path missing
            "claude": {"model": "claude-sonnet-4-20250514"},
        }
        with pytest.raises(ValueError, match="db_path"):
            _validate(config)
