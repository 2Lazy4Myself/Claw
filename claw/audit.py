"""
audit.py

Read-only diagnostic: fetches all Claw data from Todoist and cross-checks for
conflicts between goals, lifestyle habits, and the active fitness programme.

Run with:
    python -m claw.audit

Future extensions (not in scope for v1):
  - --telegram flag to send the report via Telegram (same _report_* functions,
    render to string, telegram.send_message() handles delivery)
  - Interactive fix-up: user replies "fix it" and Claw proposes + executes
    specific Todoist changes (relabelling, description edits) via a probe-style
    multi-turn loop in listener.py using update_task_labels (not yet written)
"""

from __future__ import annotations
import sys
import logging
from datetime import date

from claw.config import load_config
from claw.memory import MemoryStore
from claw.todoist_client import from_env as todoist_from_env
from claw.goals import get_goals, goal_for_task, goal_line_for_task, build_goal_summary
from claw import fitness as fitness_mod

logger = logging.getLogger(__name__)

_SEP = "-" * 60


def _h(title: str) -> None:
    print(f"\n{_SEP}")
    print(title)
    print(_SEP)


def _warn(msg: str) -> None:
    print(f"  ⚠  {msg}")


def _ok(msg: str) -> None:
    print(f"  ✓  {msg}")


# ─── Section reporters ────────────────────────────────────────────────────────

def _report_programme(programme) -> None:
    _h("ACTIVE PROGRAMME")
    if programme is None:
        _warn("No active programme found in Programmes section.")
        return

    print(f"  {programme.name}")
    week_plan = programme.weeks.get(programme.current_week)
    deload = " | DELOAD" if week_plan and week_plan.is_deload else ""
    phase = week_plan.phase if week_plan else "unknown"
    print(f"  Week {programme.current_week} | Phase: {phase}{deload}")
    print(f"  Labels: {', '.join(programme.labels) or '(none)'}")
    print(f"  Start: {programme.start_date}  Status: {programme.status}")

    if not week_plan:
        _warn(f"No week plan found for current_week={programme.current_week} — is the description up to date?")
        return

    sessions = []
    for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
        s = week_plan.sessions.get(day)
        if s:
            label = s.session_type if s.session_type else (s.exercises[0] if s.exercises else "rest")
            sessions.append(f"{day}({label})")
    print(f"  This week: {' '.join(sessions)}")


def _report_goals(goals, all_tasks) -> None:
    _h("GOALS")
    if not goals:
        print("  No goals configured.")
        return

    for goal in goals:
        labels_str = ", ".join(goal.labels) or "(none)"
        print(f"  {goal.name}  labels: {labels_str}")
        linked = [t for t in all_tasks if goal_for_task(t, [goal]) is not None]
        habits_linked = [t for t in linked if t.is_habit]
        work_linked = [t for t in linked if not t.is_habit]
        if habits_linked:
            print(f"    Linked habits:     {', '.join(t.content for t in habits_linked)}")
        if work_linked:
            print(f"    Linked work tasks: {', '.join(t.content for t in work_linked)}")
        if not linked:
            _warn(f"Goal '{goal.name}' has no linked tasks in the current pool — it is invisible to briefings.")


def _report_habits(habits, programme, goals) -> None:
    _h("LIFESTYLE HABITS")
    if not habits:
        print("  No lifestyle habits found.")
        return

    prog_labels = set(programme.labels) if programme else set()

    for habit in habits:
        labels_str = ", ".join(habit.labels) or "(none)"
        is_fitness = bool(prog_labels & set(habit.labels))
        fitness_tag = "[FITNESS ✓]" if is_fitness else "[generic]"
        print(f"  {habit.content}  labels: {labels_str}  {fitness_tag}")

        if is_fitness:
            linked_goal = goal_for_task(habit, goals)
            if linked_goal:
                shared = prog_labels & set(habit.labels) & set(linked_goal.labels)
                _warn(
                    f"Also linked to goal '{linked_goal.name}' via label(s): {', '.join(shared)}\n"
                    f"       → probe injects BOTH goal_line AND fitness_context"
                )


def _report_conflicts(habits, programme, goals) -> None:
    _h("CONFLICTS DETECTED")
    found = False
    prog_labels = set(programme.labels) if programme else set()

    # Dual-context injection
    double = []
    for habit in habits:
        if not (prog_labels & set(habit.labels)):
            continue
        linked_goal = goal_for_task(habit, goals)
        if linked_goal:
            double.append((habit, linked_goal))

    if double:
        found = True
        goal_name = double[0][1].name
        shared_label = prog_labels & set(double[0][0].labels) & set(double[0][1].labels)
        _warn(
            f"{len(double)} fitness habit(s) also linked to goal '{goal_name}' "
            f"via shared label(s): {', '.join(shared_label)}\n"
            f"     This injects goal context alongside fitness probe context.\n"
            f"     Fixed in probe.py: goal_line suppressed when is_fitness=True."
        )

    # Session types with no matching habit task
    if programme:
        week_plan = programme.weeks.get(programme.current_week)
        if week_plan:
            habit_names = {h.content.lower() for h in habits}
            for day, session in week_plan.sessions.items():
                if not session.session_type:
                    continue
                # Check if any habit name contains the session type
                match = any(session.session_type.lower() in name for name in habit_names)
                if not match:
                    found = True
                    _warn(
                        f"Session '{session.session_type}' ({day}) has no matching habit task with "
                        f"a fitness label — it will not be detected as a fitness probe."
                    )

    # Multiple active programmes
    # (already checked in _report_programme; would need programme_tasks passed in — skip here)

    if not found:
        _ok("No conflicts detected.")


def _report_context_preview(programme, goals, habits, all_tasks, memory) -> None:
    _h("CONTEXT PREVIEW")
    today = date.today()

    if programme:
        compliance = fitness_mod.get_week_compliance(programme)
        fc = fitness_mod.build_fitness_briefing_context(programme, compliance, today)
        print("\n  fitness_context (briefing):")
        for line in fc.splitlines()[:6]:
            print(f"    {line}")
        if len(fc.splitlines()) > 6:
            print(f"    ... ({len(fc.splitlines()) - 6} more lines)")
    else:
        print("\n  fitness_context: (no active programme)")

    gc = build_goal_summary(all_tasks + habits, goals, memory)
    print("\n  goal_context:")
    for line in gc.splitlines()[:5]:
        print(f"    {line}")

    prog_labels = set(programme.labels) if programme else set()
    double_habits = [h for h in habits if prog_labels & set(h.labels) and goal_for_task(h, goals)]
    if double_habits:
        print(f"\n  goal_line that was injected into fitness probes (now suppressed):")
        sample = double_habits[0]
        gl = goal_line_for_task(sample, goals)
        for line in gl.splitlines():
            print(f"    {line}")
        print(f"    (shown for '{sample.content}' — same pattern for all {len(double_habits)} fitness habits)")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    config = load_config()
    todoist = todoist_from_env()
    memory = MemoryStore(config["memory"]["db_path"])

    print(f"\n{'=' * 60}")
    print(f"CLAW AUDIT — {date.today()}")
    print(f"{'=' * 60}")

    habits, goal_tasks = todoist.get_claw_data()
    programme_tasks = todoist.get_programmes()

    all_tasks = []
    for project_key in config["todoist"]["projects"]:
        all_tasks.extend(todoist.get_today_and_overdue(project_key))

    programme = fitness_mod.get_active_programme(programme_tasks)
    goals = get_goals(goal_tasks)

    _report_programme(programme)
    _report_goals(goals, habits + all_tasks)
    _report_habits(habits, programme, goals)
    _report_conflicts(habits, programme, goals)
    _report_context_preview(programme, goals, habits, all_tasks, memory)

    print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    main()
