"""
todoist_client.py

Responsibility: Fetch tasks from the Todoist API and return normalised Task objects.

This module knows nothing about memory, Claude, or Telegram. It only knows how to
talk to Todoist and return clean data. All API-specific quirks (pagination, field
naming, date formats) are handled here so the rest of the codebase sees a stable
interface.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
import os
import requests


@dataclass
class Task:
    """
    Normalised representation of a Todoist task.

    This is the internal data model — not a Todoist API object. If Todoist changes
    their API, only this module needs updating.
    """
    id: str
    content: str
    description: str
    project_id: str
    project_name: str
    labels: list[str]
    due_date: Optional[date]
    created_at: datetime
    priority: int  # 1 (normal) to 4 (urgent), Todoist's native scale
    is_overdue: bool
    days_overdue: int

    @property
    def display_name(self) -> str:
        """Short human-readable name for logging and prompts."""
        return self.content[:80]


class TodoistClient:
    """
    Fetches and normalises tasks from the Todoist REST API v2.

    Each method returns plain data objects or raises a descriptive exception.
    No side effects beyond network calls.
    """

    BASE_URL = "https://api.todoist.com/rest/v2"

    def __init__(self, api_token: str) -> None:
        self._token = api_token
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {self._token}"})

    def get_today_tasks(
        self,
        include_project_ids: list[str] | None = None,
        exclude_labels: list[str] | None = None,
    ) -> list[Task]:
        """
        Returns all tasks due today or overdue, optionally filtered by project and label.

        Args:
            include_project_ids: If provided, only tasks in these projects are returned.
            exclude_labels: Tasks carrying any of these labels are excluded.

        Returns:
            List of Task objects, sorted by days_overdue descending then priority descending.

        Raises:
            TodoistAPIError: On non-2xx response or network failure.
        """
        raise NotImplementedError("Phase 1 implementation")

    def get_projects(self) -> dict[str, str]:
        """
        Returns a mapping of project_id -> project_name.

        Raises:
            TodoistAPIError: On non-2xx response or network failure.
        """
        raise NotImplementedError("Phase 1 implementation")

    def _parse_task(self, raw: dict, projects: dict[str, str]) -> Task:
        """
        Converts a raw Todoist API task dict into a Task dataclass.

        This is the only place we touch Todoist's field names and date formats.
        """
        raise NotImplementedError("Phase 1 implementation")

    def _calculate_overdue(self, due_date: Optional[date]) -> tuple[bool, int]:
        """
        Returns (is_overdue, days_overdue) relative to today.
        Tasks with no due date are never overdue.
        """
        raise NotImplementedError("Phase 1 implementation")


class TodoistAPIError(Exception):
    """Raised when the Todoist API returns an error or is unreachable."""
    pass


def from_env() -> TodoistClient:
    """
    Convenience factory that reads TODOIST_API_TOKEN from the environment.

    Raises:
        EnvironmentError: If the token is not set.
    """
    token = os.environ.get("TODOIST_API_TOKEN")
    if not token:
        raise EnvironmentError("TODOIST_API_TOKEN is not set")
    return TodoistClient(token)
