"""
todoist_client.py

Fetches tasks from Todoist using section-based temporal bucketing.

Jake's Todoist uses sections — not due dates — as the primary time signal.
Moving a task to a section (Today / Next 2-3 Days / This Week etc.) IS the
planning gesture. due_date is secondary: used to flag overdue items and to
pull tasks into the Today bucket regardless of section.

This mirrors the approach in the parent todoist-telegram project on Unraid.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Optional
import os
import requests


# ─── Project & section definitions ───────────────────────────────────────────

PROJECTS: dict[str, dict[str, str]] = {
    "work": {
        "project_id": "6RmFxHccRw63CJ94",
        "TODAY":       "6Rmj8j24Mp77RwHW",
        "NEXT_FEW":    "6Rmj8jq5vMwhrmfW",
        "THIS_WEEK":   "6Rmj8mJVcR9JhRpW",
        "NEXT_WEEK":   "6V63Gr28FH8H2q54",
        "THIS_MONTH":  "6V63GxJh2mFj3MX4",
        "WAITING":     "6Rp3829965pCcVVW",
        "UNPROCESSED": "6Rp374jhgfgP3prW",
    },
    "home": {
        "project_id": "6RmFxHWHv3f2p9Cr",
        "TODAY":       "6WMh74gr9RrqmqCJ",
        "NEXT_FEW":    "6c39w73rGgjv965r",
        "THIS_WEEK":   "6gXj6JCHxM57pxxr",
        "NEXT_WEEK":   "6gXj6JJ9hFcQCH9J",
        "THIS_MONTH":  "6gXj6JPmV8xmcHVJ",
        "WAITING":     "6gXj6J8gq3xH435J",
        "UNPROCESSED": "6gXj6jqqqGmvp6Vr",
    },
    "claw": {
        "project_id": "6gW4jC59ChjCjpFG",
        "LIFESTYLE":  "6gfjQXwJfGRVvJGG",
    },
}

# Section IDs that mark a task as a lifestyle habit rather than a regular task.
HABIT_SECTIONS: set[str] = {
    "6gfjQXwJfGRVvJGG",  # Claw / Life Style
}

SECTION_DISPLAY: dict[str, str] = {
    "TODAY":       "Today",
    "NEXT_FEW":    "Next 2-3 Days",
    "THIS_WEEK":   "This Week",
    "NEXT_WEEK":   "Next Week",
    "THIS_MONTH":  "This Month",
    "WAITING":     "Waiting For",
    "UNPROCESSED": "Unprocessed",
}

# Tasks in these sections are never surfaced to Claw.
IGNORED_SECTIONS = {"Routines 🔁", "Inspiration ✨", "Later"}


# ─── Data model ──────────────────────────────────────────────────────────────

@dataclass
class Task:
    """
    Normalised Todoist task.

    section_name is the primary temporal signal: "Today", "Next 2-3 Days",
    "This Week", etc. due_date is present on some tasks but its absence does
    not mean a task has no temporal intent — section carries that meaning.
    """
    id: str
    content: str
    description: str
    project_id: str
    project_name: str   # "work", "home", or "claw"
    section_id: str
    section_name: str   # Human-readable section: "Today", "Next 2-3 Days", etc.
    labels: list[str]
    due_date: Optional[date]
    priority: int       # 1 (normal) → 4 (urgent), Todoist native scale
    is_overdue: bool    # due_date is in the past
    days_overdue: int   # 0 if not overdue
    is_habit: bool      # True if this is a lifestyle habit (Life Style section)

    @property
    def display_name(self) -> str:
        return self.content[:80]


# ─── Client ──────────────────────────────────────────────────────────────────

class TodoistClient:
    """
    Fetches tasks from Todoist REST API v1 with section-based organisation.
    """

    BASE_URL = "https://api.todoist.com/api/v1"

    def __init__(self, api_token: str) -> None:
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_token}"

    def get_tasks_for_project(self, project_key: str) -> list[Task]:
        """
        All non-completed tasks for a project, with section names resolved.
        Excludes ignored sections (Routines, Inspiration, Later).
        """
        project = self._project(project_key)
        section_map = self._section_map(project_key)
        today = date.today()

        raw = self._fetch_all(f"{self.BASE_URL}/tasks", {"project_id": project["project_id"]})
        tasks = []
        for r in raw:
            if r.get("is_completed"):
                continue
            section_name = section_map.get(r.get("section_id", ""), "")
            if section_name in IGNORED_SECTIONS:
                continue
            tasks.append(self._parse(r, project_key, section_name, today))
        return tasks

    def get_today_and_overdue(self, project_key: str) -> list[Task]:
        """
        Tasks in the Today section, plus any task with due_date <= today.
        Mirrors todoist_fetcher.js getTodayAndOverdueTasks.
        """
        all_tasks = self.get_tasks_for_project(project_key)
        today_section_id = self._project(project_key)["TODAY"]
        today_str = date.today().isoformat()

        seen: set[str] = set()
        results = []
        for task in all_tasks:
            in_today_section = task.section_id == today_section_id
            due_today_or_past = task.due_date is not None and task.due_date.isoformat() <= today_str
            if in_today_section or due_today_or_past:
                if task.id not in seen:
                    seen.add(task.id)
                    results.append(task)
        return results

    def get_lifestyle_habits(self) -> list[Task]:
        """
        All non-completed tasks in the Life Style section of the Claw project.
        These are ongoing habits, not one-off tasks.
        """
        project = self._project("claw")
        section_map = self._section_map("claw")
        today = date.today()
        raw = self._fetch_all(
            f"{self.BASE_URL}/tasks", {"project_id": project["project_id"]}
        )
        return [
            self._parse(r, "claw", section_map.get(r.get("section_id", ""), ""), today)
            for r in raw
            if not r.get("is_completed")
            and r.get("section_id") == project["LIFESTYLE"]
        ]

    def close_task(self, task_id: str) -> None:
        """Marks a task complete in Todoist. Works for tasks and subtasks."""
        try:
            resp = self._session.post(
                f"{self.BASE_URL}/tasks/{task_id}/close", timeout=10
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise TodoistAPIError(str(exc)) from exc

    def get_subtasks(self, task_id: str) -> list[Task]:
        """Returns all non-completed subtasks of a given task."""
        today = date.today()
        raw = self._fetch_all(f"{self.BASE_URL}/tasks", {"parent_id": task_id})
        return [
            self._parse(r, "", "", today)
            for r in raw
            if not r.get("is_completed")
        ]

    def update_task_description(self, task_id: str, new_description: str) -> None:
        """
        Replaces a task's description via the Todoist API.
        Used to append timestamped habit log entries after a probe conversation.
        """
        try:
            resp = self._session.post(
                f"{self.BASE_URL}/tasks/{task_id}",
                json={"description": new_description},
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise TodoistAPIError(str(exc)) from exc

    def get_waiting_for(self, project_key: str) -> list[Task]:
        """Tasks in the Waiting For section."""
        wid = self._project(project_key)["WAITING"]
        return [t for t in self.get_tasks_for_project(project_key) if t.section_id == wid]

    def get_unprocessed(self, project_key: str) -> list[Task]:
        """Tasks in the Unprocessed section."""
        uid = self._project(project_key)["UNPROCESSED"]
        return [t for t in self.get_tasks_for_project(project_key) if t.section_id == uid]

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _project(self, project_key: str) -> dict[str, str]:
        if project_key not in PROJECTS:
            raise ValueError(f"Unknown project: {project_key!r}. Valid: {list(PROJECTS)}")
        return PROJECTS[project_key]

    def _section_map(self, project_key: str) -> dict[str, str]:
        """section_id → display name for a given project."""
        return {
            sid: SECTION_DISPLAY[key]
            for key, sid in PROJECTS[project_key].items()
            if key != "project_id" and key in SECTION_DISPLAY
        }

    def _fetch_all(self, url: str, params: dict) -> list[dict]:
        """Cursor-paginated fetch against Todoist API v1."""
        results: list[dict] = []
        cursor: Optional[str] = None
        while True:
            p = {**params, **({"cursor": cursor} if cursor else {})}
            try:
                resp = self._session.get(url, params=p, timeout=10)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise TodoistAPIError(str(exc)) from exc
            data = resp.json()
            results.extend(data.get("results", data) if isinstance(data, dict) else data)
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor:
                break
        return results

    def _parse(self, raw: dict, project_key: str, section_name: str, today: date) -> Task:
        due = raw.get("due")
        due_date: Optional[date] = None
        if due and due.get("date"):
            try:
                due_date = date.fromisoformat(due["date"][:10])
            except ValueError:
                pass

        is_overdue = due_date is not None and due_date < today
        days_overdue = (today - due_date).days if is_overdue else 0
        section_id = raw.get("section_id", "")

        return Task(
            id=raw["id"],
            content=raw.get("content", ""),
            description=raw.get("description", ""),
            project_id=raw.get("project_id", ""),
            project_name=project_key,
            section_id=section_id,
            section_name=section_name,
            labels=raw.get("labels", []),
            due_date=due_date,
            priority=raw.get("priority", 1),
            is_overdue=is_overdue,
            days_overdue=days_overdue,
            is_habit=section_id in HABIT_SECTIONS,
        )


# ─── Errors & factories ───────────────────────────────────────────────────────

class TodoistAPIError(Exception):
    """Raised when the Todoist API returns an error or is unreachable."""


def from_env() -> TodoistClient:
    """Reads TODOIST_API_TOKEN from the environment."""
    token = os.environ.get("TODOIST_API_TOKEN")
    if not token:
        raise EnvironmentError("TODOIST_API_TOKEN is not set")
    return TodoistClient(token)
