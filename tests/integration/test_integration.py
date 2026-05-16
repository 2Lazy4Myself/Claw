"""
Integration tests for Claw.

These tests make real API calls and require credentials in the environment.
They are tagged with @pytest.mark.integration and excluded from standard CI runs.

Run with: pytest tests/integration/ -m integration
Requires: .env file with all three API keys set.

These tests are intentionally narrow — they verify connectivity and basic
response shape, not the content of Claude's responses.
"""

import pytest
import os
from dotenv import load_dotenv

load_dotenv()


# Skip all integration tests if credentials are missing
def credentials_present():
    return all([
        os.environ.get("ANTHROPIC_API_KEY"),
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        os.environ.get("TODOIST_API_TOKEN"),
    ])


skip_if_no_creds = pytest.mark.skipif(
    not credentials_present(),
    reason="Integration credentials not set in environment"
)


@pytest.mark.integration
@skip_if_no_creds
class TestTodoistIntegration:
    def test_can_fetch_projects(self):
        from claw.todoist_client import from_env
        client = from_env()
        projects = client.get_projects()
        assert isinstance(projects, dict)

    def test_can_fetch_today_tasks(self):
        from claw.todoist_client import from_env
        client = from_env()
        tasks = client.get_today_tasks()
        assert isinstance(tasks, list)
        # Each item should be a Task with expected fields
        for task in tasks:
            assert hasattr(task, "id")
            assert hasattr(task, "content")
            assert hasattr(task, "is_overdue")


@pytest.mark.integration
@skip_if_no_creds
class TestClaudeIntegration:
    def test_single_turn_completion_returns_string(self):
        from claw.claude_client import ClaudeClient
        config = {"claude": {"model": "claude-sonnet-4-20250514"}}
        client = ClaudeClient.from_env(config)
        result = client.complete(
            system="You are a helpful assistant. Reply in 5 words or fewer.",
            user="Say hello.",
            max_tokens=50,
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_task_selection_returns_parseable_json(self):
        """Verifies the task selection prompt returns valid JSON."""
        import json
        from claw.claude_client import ClaudeClient
        from claw import prompts

        config = {"claude": {"model": "claude-sonnet-4-20250514"}}
        client = ClaudeClient.from_env(config)

        result = client.complete(
            system=prompts.get_prompt("TASK_SELECTION_SYSTEM"),
            user=prompts.TASK_SELECTION_USER_TEMPLATE.format(
                task_list_with_memory=(
                    "- task_id: abc123, content: 'Update website copy', "
                    "overdue: 5 days, last_probed: 7 days ago, notes: 'User said they'd do it'"
                )
            ),
            max_tokens=150,
        )
        parsed = json.loads(result)
        assert "task_id" in parsed
        assert "reason" in parsed


@pytest.mark.integration
@skip_if_no_creds
class TestMemoryIntegration:
    def test_memory_roundtrip(self, tmp_path):
        """Write and read back a TaskMemory record."""
        from claw.memory import MemoryStore, TaskMemory
        from datetime import datetime

        db_path = str(tmp_path / "test_claw.db")
        store = MemoryStore(db_path)

        memory = TaskMemory(
            task_id="test-task-001",
            last_probed_at=datetime.now(),
            probe_count=1,
            last_outcome="user_committed",
            notes="Said they'd do it Thursday.",
            snoozed_until=None,
        )
        store.upsert_task_memory(memory)

        retrieved = store.get_task_memory("test-task-001")
        assert retrieved is not None
        assert retrieved.task_id == "test-task-001"
        assert retrieved.probe_count == 1
        assert retrieved.last_outcome == "user_committed"

    def test_missing_task_returns_none(self, tmp_path):
        from claw.memory import MemoryStore
        db_path = str(tmp_path / "test_claw.db")
        store = MemoryStore(db_path)
        result = store.get_task_memory("nonexistent-id")
        assert result is None
