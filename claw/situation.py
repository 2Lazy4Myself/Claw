"""
situation.py

Responsibility: assemble a "where we are right now" snapshot — current fitness
programme position and goal progress — for injection into open chat.

Briefings and probes already see programme/goal context; general chat did not,
so when the user just messaged Claw it had no idea where they were in their
programme. This module reuses the existing fitness/goal builders to close that
gap, and exposes the shared Todoist fetch so the listener's free-form matcher and
conversational fallback don't fetch twice for one message.
"""

from __future__ import annotations
from datetime import date

from claw.goals import get_goals, build_goal_summary, GoalRecord
from claw.memory import MemoryStore
from claw.todoist_client import TodoistClient
from claw import fitness as fitness_mod


def gather_active_context(
    todoist: TodoistClient, config: dict
) -> tuple[list, list, list[GoalRecord], object]:
    """
    Fetch the live task/habit/goal/programme snapshot used by the chat path.

    Returns (today_and_overdue_tasks, habits, goals, active_programme).
    Mirrors the fetch the briefing and free-form update paths already perform.
    """
    all_tasks: list = []
    for project_key in config["todoist"]["projects"]:
        all_tasks.extend(todoist.get_today_and_overdue(project_key))
    habits, goal_tasks = todoist.get_claw_data()
    goals = get_goals(goal_tasks)
    active_programme = fitness_mod.get_active_programme(todoist.get_programmes())
    return all_tasks, habits, goals, active_programme


def build_situation_snapshot(
    all_tasks: list,
    habits: list,
    goals: list[GoalRecord],
    active_programme: object,
    memory: MemoryStore,
    today: date,
) -> str:
    """
    Render a compact current-situation block from already-fetched context.

    Pure formatting (no Todoist fetch) so callers that already gathered context
    can reuse it. Returns "" when there's nothing to say.
    """
    parts: list[str] = []

    if active_programme is not None:
        compliance = fitness_mod.get_week_compliance(active_programme)
        parts.append(fitness_mod.build_fitness_briefing_context(active_programme, compliance, today))

    goal_summary = build_goal_summary(all_tasks + habits, goals, memory)
    if goal_summary and goal_summary != "No goals configured.":
        parts.append("Goals:\n" + goal_summary)

    return "\n\n".join(parts)
