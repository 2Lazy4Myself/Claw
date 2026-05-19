"""
goals.py

Responsibility: Goal definitions, task→goal mapping, and goal activity context.

Goals live in Todoist — a "Goals" section in the Claw project. Each goal is a task
whose description follows a structured Key: Value template:

    Why: Feel confident, lighter, more energy day to day
    Target: 85kg
    Current: 108kg
    By: 2026-12-01
    Status: Diet consistent, exercise still patchy

Labels on the goal task and on work/home tasks are the linking mechanism.
A task is assigned to a goal if any of its labels match any of the goal's labels.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from claw.todoist_client import TodoistClient

from claw.todoist_client import Task
from claw.memory import MemoryStore


@dataclass
class GoalRecord:
    task_id: str
    name: str            # Todoist task content
    labels: list[str]    # Todoist labels — the linking mechanism
    why: str             # Motivational anchor
    target: Optional[str]   # Measurable target, e.g. "85kg" or "100cm"
    current: Optional[str]  # Current measurement — updated by Claw from conversation
    by: Optional[date]      # Optional deadline
    status: str             # Freeform note about current state


def parse_goal_description(description: str) -> dict:
    """
    Parses Key: Value lines from a goal task description.
    Case-insensitive. Missing fields return empty string or None.
    Never raises — unknown lines are silently skipped.
    """
    result: dict = {"why": "", "target": None, "current": None, "by": None, "status": ""}

    for line in description.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key_norm = key.strip().lower()
        val_norm = val.strip()

        if key_norm == "why":
            result["why"] = val_norm
        elif key_norm == "target":
            result["target"] = val_norm or None
        elif key_norm == "current":
            result["current"] = val_norm or None
        elif key_norm == "by":
            try:
                result["by"] = date.fromisoformat(val_norm)
            except (ValueError, TypeError):
                pass
        elif key_norm == "status":
            result["status"] = val_norm

    return result


def get_goals(todoist: "TodoistClient") -> list[GoalRecord]:
    """Fetches goals from the Todoist Goals section and parses their descriptions."""
    tasks = todoist.get_goals()
    goals = []
    for task in tasks:
        fields = parse_goal_description(task.description or "")
        goals.append(GoalRecord(
            task_id=task.id,
            name=task.content,
            labels=task.labels,
            why=fields["why"],
            target=fields["target"],
            current=fields["current"],
            by=fields["by"],
            status=fields["status"],
        ))
    return goals


def goal_for_task(task: Task, goals: list[GoalRecord]) -> Optional[GoalRecord]:
    """Returns the first goal whose labels overlap with the task's labels, or None."""
    task_labels = set(task.labels)
    for goal in goals:
        if task_labels & set(goal.labels):
            return goal
    return None


def build_goal_summary(
    tasks: list[Task], goals: list[GoalRecord], memory: MemoryStore
) -> str:
    """
    Returns a plain-text block summarising goal state for prompt injection.

    For each goal: progress (current → target), last activity from memory,
    and QUIET flag if silent for 7+ days.
    """
    if not goals:
        return "No goals configured."

    lines = []
    for goal in goals:
        goal_tasks = [t for t in tasks if goal_for_task(t, [goal]) is not None]

        # Last activity across all linked tasks
        last_activity: Optional[datetime] = None
        for t in goal_tasks:
            tm = memory.get_task_memory(t.id)
            if tm and tm.last_probed_at:
                probed = tm.last_probed_at
                if probed.tzinfo is None:
                    probed = probed.replace(tzinfo=timezone.utc)
                if last_activity is None or probed > last_activity:
                    last_activity = probed

        if last_activity is None:
            silence = "never discussed"
            flag = " ← QUIET"
        else:
            days = (datetime.now(timezone.utc) - last_activity).days
            if days == 0:
                silence, flag = "discussed today", ""
            elif days == 1:
                silence, flag = "discussed yesterday", ""
            elif days <= 6:
                silence, flag = f"discussed {days} days ago", ""
            else:
                silence, flag = f"last discussed {days} days ago", " ← QUIET"

        # Progress string
        if goal.current and goal.target:
            progress = f"{goal.current} → {goal.target}"
        elif goal.target:
            progress = f"target: {goal.target}"
        else:
            progress = ""

        summary = f"- {goal.name}"
        if progress:
            summary += f" ({progress})"
        summary += f": {silence}{flag}"
        if goal.why:
            summary += f". Why: {goal.why}"

        lines.append(summary)

    return "\n".join(lines)


def goal_line_for_task(task: Task, goals: list[GoalRecord]) -> str:
    """
    Returns a multi-line goal context block for probe prompt injection.
    Empty string if the task has no linked goal.
    """
    goal = goal_for_task(task, goals)
    if goal is None:
        return ""

    lines = [f"Goal this task serves: {goal.name}"]

    if goal.current and goal.target:
        lines.append(f"Progress: {goal.current} → {goal.target}")
    elif goal.target:
        lines.append(f"Target: {goal.target}")

    if goal.by:
        days_left = (goal.by - date.today()).days
        if days_left >= 0:
            lines.append(f"Deadline: {goal.by.strftime('%-d %b %Y')} ({days_left} days away)")
        else:
            lines.append(f"Deadline: {goal.by.strftime('%-d %b %Y')} (overdue by {-days_left} days)")

    if goal.why:
        lines.append(f"Why it matters: {goal.why}")

    if goal.status:
        lines.append(f"Status: {goal.status}")

    return "\n".join(lines)
