"""
watchlist.py

Responsibility: Detect topics (fitness, goals, habits) that have been silent for
too long and surface them for proactive check-ins.

The watchlist is queried at the start of each probe cycle and by the listener when
processing free-form messages. A topic is "overdue" if the most recent task_memory
last_probed_at (or most recent fitness log entry) exceeds silence_threshold_days.

No SQLite writes here — this module is read-only on the memory store.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Optional, TYPE_CHECKING

from claw.goals import GoalRecord, goal_for_task
from claw.memory import MemoryStore
from claw.todoist_client import Task

if TYPE_CHECKING:
    from claw.fitness import Programme

logger = logging.getLogger(__name__)


@dataclass
class OverdueTopic:
    topic_type: str    # "fitness" | "goal" | "habit"
    topic_name: str    # human-readable label for messages and matching
    days_silent: int
    task: Task         # anchor task for probe and post-conversation detection


def get_overdue_topics(
    memory: MemoryStore,
    all_tasks: list[Task],
    goals: list[GoalRecord],
    programme: Optional["Programme"],
    config: dict,
    today: date,
) -> list[OverdueTopic]:
    """
    Returns topics silent for >= silence_threshold_days, sorted most-overdue first.

    Fitness: measured by last programme log entry date.
    Goals: max(last_probed_at) across all tasks sharing any label with the goal.
    Habits: last_probed_at of the habit task itself.

    Tasks already covered by a higher-priority topic (fitness > goal > habit) are
    excluded to avoid generating duplicate check-ins for the same underlying task.
    """
    threshold = config.get("watchlist", {}).get("silence_threshold_days", 7)
    if threshold <= 0:
        return []

    all_task_ids = [t.id for t in all_tasks]
    memories = memory.get_task_memories(all_task_ids)
    topics: list[OverdueTopic] = []
    covered_ids: set[str] = set()

    # ── Fitness ──────────────────────────────────────────────────────────────
    if programme is not None:
        days = _fitness_days_silent(programme, today)
        if days >= threshold:
            anchor = _pick_fitness_anchor(all_tasks, programme)
            if anchor is not None:
                topics.append(OverdueTopic(
                    topic_type="fitness",
                    topic_name=programme.name,
                    days_silent=days,
                    task=anchor,
                ))
                covered_ids.add(anchor.id)

    # ── Goals ─────────────────────────────────────────────────────────────────
    for goal in goals:
        linked = [t for t in all_tasks if goal_for_task(t, [goal]) is not None]
        if not linked:
            continue

        last_activity: Optional[datetime] = None
        for t in linked:
            tm = memories.get(t.id)
            if tm and tm.last_probed_at:
                probed = tm.last_probed_at
                if probed.tzinfo is None:
                    probed = probed.replace(tzinfo=timezone.utc)
                if last_activity is None or probed > last_activity:
                    last_activity = probed

        days = (datetime.now(timezone.utc) - last_activity).days if last_activity else 9999
        if days < threshold:
            continue

        anchor = _pick_oldest_task(linked, memories)
        if anchor is None or anchor.id in covered_ids:
            continue

        topics.append(OverdueTopic(
            topic_type="goal",
            topic_name=goal.name,
            days_silent=days,
            task=anchor,
        ))
        covered_ids.add(anchor.id)

    # ── Habits ────────────────────────────────────────────────────────────────
    for task in all_tasks:
        if not task.is_habit or task.id in covered_ids:
            continue

        tm = memories.get(task.id)
        if tm and tm.last_probed_at:
            probed = tm.last_probed_at
            if probed.tzinfo is None:
                probed = probed.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - probed).days
        else:
            days = 9999

        if days < threshold:
            continue

        topics.append(OverdueTopic(
            topic_type="habit",
            topic_name=task.content,
            days_silent=days,
            task=task,
        ))
        covered_ids.add(task.id)

    return sorted(topics, key=lambda t: -t.days_silent)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fitness_days_silent(programme: "Programme", today: date) -> int:
    """Days since the last programme log entry, or 9999 if the log is empty."""
    from claw.fitness import get_last_log_date
    last = get_last_log_date(programme)
    return (today - last).days if last else 9999


def _pick_fitness_anchor(all_tasks: list[Task], programme: "Programme") -> Optional[Task]:
    """Picks the first fitness habit task matching the programme's labels."""
    for task in all_tasks:
        if task.is_habit and any(label in task.labels for label in programme.labels):
            return task
    return None


def _pick_oldest_task(tasks: list[Task], memories: dict) -> Optional[Task]:
    """Returns the task with the oldest (or absent) last_probed_at."""
    if not tasks:
        return None

    def sort_key(t: Task) -> datetime:
        tm = memories.get(t.id)
        if tm and tm.last_probed_at:
            dt = tm.last_probed_at
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        return datetime.min.replace(tzinfo=timezone.utc)

    return sorted(tasks, key=sort_key)[0]
