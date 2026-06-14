"""
Unit tests for task capture from chat (F2).

Covers the Todoist create_task payload and the listener capture handler
(extraction → create → confirm), with the model and Todoist mocked.

Run with: pytest tests/unit/
"""

import json
from unittest.mock import MagicMock

import pytest

from claw import listener
from claw.todoist_client import TodoistClient


def _config():
    return {
        "claude": {"selection_model": "sel"},
        "behaviour": {"capture_default_project": "home", "capture_default_section": "TODAY"},
    }


class TestCreateTask:
    def test_builds_payload_and_returns_id(self):
        client = TodoistClient.__new__(TodoistClient)  # bypass __init__/session
        resp = MagicMock()
        resp.json.return_value = {"id": "task-99"}
        client._request_with_retry = MagicMock(return_value=resp)

        new_id = client.create_task("Call dentist", "home", "TODAY")

        assert new_id == "task-99"
        _, kwargs = client._request_with_retry.call_args[0], client._request_with_retry.call_args[1]
        payload = kwargs["json"]
        assert payload["content"] == "Call dentist"
        assert payload["project_id"]  # resolved from PROJECTS["home"]
        assert payload["section_id"]  # resolved from PROJECTS["home"]["TODAY"]
        assert "description" not in payload  # omitted when empty

    def test_unknown_section_raises(self):
        client = TodoistClient.__new__(TodoistClient)
        with pytest.raises(ValueError, match="Unknown section"):
            client.create_task("x", "home", "NOPE")


class TestHandleCapture:
    def _claude_returning(self, obj):
        claude = MagicMock()
        claude.complete.return_value = json.dumps(obj)
        return claude

    def test_creates_task_and_confirms(self):
        todoist = MagicMock()
        telegram = MagicMock()
        claude = self._claude_returning(
            {"content": "Book MOT", "project": "home", "section": "NEXT_WEEK"}
        )

        listener._handle_capture("I need to book an MOT next week", todoist, claude, telegram, _config())

        todoist.create_task.assert_called_once_with("Book MOT", "home", "NEXT_WEEK")
        sent = telegram.send_message.call_args.args[0]
        assert "Book MOT" in sent and "Next Week" in sent  # section display name

    def test_unknown_project_section_fall_back_to_defaults(self):
        todoist = MagicMock()
        claude = self._claude_returning(
            {"content": "Do thing", "project": "garage", "section": "WHENEVER"}
        )

        listener._handle_capture("do thing", todoist, claude, MagicMock(), _config())

        todoist.create_task.assert_called_once_with("Do thing", "home", "TODAY")

    def test_non_json_extraction_reports_failure(self):
        todoist = MagicMock()
        telegram = MagicMock()
        claude = MagicMock()
        claude.complete.return_value = "sorry, not json"

        listener._handle_capture("remind me", todoist, claude, telegram, _config())

        todoist.create_task.assert_not_called()
        assert "Couldn't add" in telegram.send_message.call_args.args[0]

    def test_empty_content_reports_failure(self):
        todoist = MagicMock()
        telegram = MagicMock()
        claude = self._claude_returning({"content": "  ", "project": "home", "section": "TODAY"})

        listener._handle_capture("uhh", todoist, claude, telegram, _config())

        todoist.create_task.assert_not_called()
        assert "Couldn't add" in telegram.send_message.call_args.args[0]

    def test_routes_capture_intent(self, monkeypatch):
        handle = MagicMock()
        monkeypatch.setattr(listener, "_handle_capture", handle)
        claude = MagicMock()
        claude.complete.return_value = '{"intent": "capture"}'

        listener._handle_message(
            "remind me to call mum", MagicMock(), MagicMock(), claude,
            MagicMock(), _config(),
        )

        handle.assert_called_once()
