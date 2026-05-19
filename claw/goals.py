"""
goals.py

Responsibility: Goal definitions, task→goal mapping, and goal activity context.

Goals are defined in config.yaml under the 'goals' key. Each goal maps to one
or more Todoist label names. A task is assigned to a goal if any of its labels
match. The goal layer gives Claude awareness of longer-term intent — so it can
notice when a goal has gone quiet even if individual tasks look fine.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from claw.todoist_client import Task
from claw.memory import MemoryStore


@dataclass
class Goal:
    name: str
    labels: list[str]      # Todoist label names that map to this goal
    description: str       # One-line summary of what this goal is about


def load_goals(config: dict) -> list[Goal]:
    """Loads goals from config. Returns empty list if no goals configured."""
    return [
        Goal(
            name=g["name"],
            labels=g.get("labels", []),
            description=g.get("description", ""),
        )
        for g in config.get("goals", [])
    ]


def goal_for_task(task: Task, goals: list[Goal]) -> Optional[Goal]:
    """Returns the first goal whose labels overlap with the task's labels, or None."""
    task_labels = set(task.labels)
    for goal in goals:
        if task_labels & set(goal.labels):
            return goal
    return None


def build_goal_summary(tasks: list[Task], goals: list[Goal], memory: MemoryStore) -> str:
    """
    Returns a plain-text block summarising goal activity for prompt injection.

    For each goal: how many tasks are in the current pool, and when any of them
    were last discussed. Goals silent for 7+ days are flagged QUIET.
    """
    if not goals:
        return "No goals configured."

    lines = []
    for goal in goals:
        goal_tasks = [t for t in tasks if goal_for_task(t, [goal]) is not None]

        if not goal_tasks:
            lines.append(f"- {goal.name}: no tasks in current pool")
            continue

        last_activity: Optional[datetime] = None
        for t in goal_tasks:
            tm = memory.get_task_memory(t.id)
            if tm and tm.last_probed_at:
                probed = tm.last_probed_at
                if probed.tzinfo is None:
                    probed = probed.replace(tzinfo=timezone.utc)
                if last_activity is None or probed > last_activity:
                    last_activity = probed

        task_preview = ", ".join(t.content for t in goal_tasks[:2])
        if len(goal_tasks) > 2:
            task_preview += f" (+{len(goal_tasks) - 2} more)"

        if last_activity is None:
            silence_str = "never discussed"
            quiet_flag = " ← QUIET"
        else:
            days = (datetime.now(timezone.utc) - last_activity).days
            if days == 0:
                silence_str = "discussed today"
                quiet_flag = ""
            elif days == 1:
                silence_str = "discussed yesterday"
                quiet_flag = ""
            elif days <= 3:
                silence_str = f"discussed {days} days ago"
                quiet_flag = ""
            elif days <= 6:
                silence_str = f"last discussed {days} days ago"
                quiet_flag = ""
            else:
                silence_str = f"last discussed {days} days ago"
                quiet_flag = " ← QUIET"

        lines.append(
            f"- {goal.name} ({len(goal_tasks)} task(s), {silence_str}{quiet_flag}): {task_preview}"
        )

    return "\n".join(lines)


def goal_line_for_task(task: Task, goals: list[Goal]) -> str:
    """
    Returns a one-line goal context string for the probe prompt, or empty string
    if the task doesn't map to any goal.
    """
    goal = goal_for_task(task, goals)
    if goal is None:
        return ""
    desc = f" — {goal.description}" if goal.description else ""
    return f"Goal this task serves: {goal.name}{desc}"
