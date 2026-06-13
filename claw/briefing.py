"""
briefing.py

Responsibility: Orchestrate the morning briefing flow.

Flow:
    1. Load config
    2. Fetch today's tasks from Todoist (all configured projects)
    3. Fetch recent memory context
    4. Ask Claude to write the briefing
    5. Send via Telegram
    6. Log the session
"""

from __future__ import annotations
import logging
import sys
import uuid
from datetime import datetime, date as _date, timezone

from claw.claude_client import ClaudeClient
from claw.config import load_config
from claw import fitness as fitness_mod
from claw.goals import get_goals, build_goal_summary
from claw.memory import MemoryStore, SessionRecord, build_context_block
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, Task, from_env as todoist_from_env
from claw import prompts

logger = logging.getLogger(__name__)


def run_briefing(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    """
    Runs one complete morning briefing cycle.

    All dependencies are passed in (not constructed here) so this function
    is testable with fakes/stubs without touching the network.
    """
    logger.info("Starting morning briefing")
    started_at = datetime.now(timezone.utc)

    # 1. Fetch tasks from all configured projects
    all_tasks: list[Task] = []
    for project_key in config["todoist"]["projects"]:
        all_tasks.extend(todoist.get_today_and_overdue(project_key))
    logger.info(f"Fetched {len(all_tasks)} tasks from Todoist")

    habits, goal_tasks = todoist.get_claw_data()
    logger.info(f"Fetched {len(habits)} lifestyle habits, {len(goal_tasks)} goals")

    waiting_tasks: list[Task] = []
    for project_key in config["todoist"]["projects"]:
        waiting_tasks.extend(todoist.get_waiting_for(project_key))
    logger.info(f"Fetched {len(waiting_tasks)} waiting-for tasks")

    if not all_tasks:
        logger.info("No tasks today — sending all-clear")
        telegram.send_message("Nothing on the board today. Enjoy the space.")
        return

    # 2. Fetch recent memory context
    recent_sessions = memory.get_recent_sessions(
        n=config["memory"]["recent_sessions_to_include"]
    )
    memory_context = build_context_block(None, recent_sessions)

    # 3. Build task list string for prompt
    task_list = _format_tasks_for_prompt(all_tasks, config["behaviour"]["briefing_max_tasks"])
    waiting_summary = _format_waiting_for_prompt(waiting_tasks)

    goals = get_goals(goal_tasks)
    goal_context = build_goal_summary(all_tasks + habits + waiting_tasks, goals, memory)

    # 4. Fitness programme context
    today = _date.today()
    programme_tasks = todoist.get_programmes()
    programme = fitness_mod.get_active_programme(programme_tasks)
    if programme:
        if fitness_mod.should_advance_week(programme, today):
            fitness_mod.advance_week(programme, todoist)
            programme.current_week += 1
        compliance = fitness_mod.get_week_compliance(programme)
        fitness_context = fitness_mod.build_fitness_briefing_context(programme, compliance, today)
        urgency = fitness_mod.compliance_urgency(compliance)
        # Exclude fitness habits from the generic habit summary — they're covered by fitness_context.
        # Keeping them in causes Claude to read day-labels like "(Tue)" in habit names as today's date.
        fitness_labels = set(programme.labels)
        non_fitness_habits = [h for h in habits if not any(l in fitness_labels for l in h.labels)]
    else:
        fitness_context = "(no active fitness programme)"
        urgency = "normal"
        non_fitness_habits = habits

    if urgency == "urgent":
        fitness_urgency_note = (
            "NOTE: Three or more fitness sessions missed this week. "
            "Don't try to catch up — adapt and move forward."
        )
    elif urgency == "flagged":
        fitness_urgency_note = (
            "NOTE: Two fitness sessions missed this week — "
            "the path back matters more than the gap."
        )
    else:
        fitness_urgency_note = ""

    habit_summary = _format_habits_for_prompt(non_fitness_habits)

    # 5. Ask Claude for the briefing
    user_profile = memory.get_user_profile()
    user_profile_block = f"User profile:\n{user_profile}\n\n" if user_profile else ""
    briefing_text = claude.complete(
        system=prompts.get_prompt("BRIEFING_SYSTEM"),
        user=prompts.BRIEFING_USER_TEMPLATE.format(
            user_profile=user_profile_block,
            task_list=task_list,
            habit_summary=habit_summary,
            waiting_summary=waiting_summary,
            goal_context=goal_context,
            memory_context=memory_context,
            fitness_context=fitness_context,
            fitness_urgency_note=fitness_urgency_note,
        ),
        max_tokens=config["claude"]["briefing_max_tokens"],
    )
    logger.info("Received briefing from Claude")

    # 6. Send via Telegram
    telegram.send_message(briefing_text)
    logger.info("Briefing sent")

    # 7. Log the session
    memory.log_session(SessionRecord(
        session_id=str(uuid.uuid4()),
        session_type="briefing",
        started_at=started_at,
        task_id=None,
        engagement_signal=None,
        summary=None,
        raw_transcript=None,
    ))


def _format_tasks_for_prompt(tasks: list[Task], max_tasks: int) -> str:
    """
    Converts a list of Task objects to a plain-text block for prompt injection.
    Caps at max_tasks. Pure function — no I/O.
    """
    if not tasks:
        return "No tasks today."

    lines = []
    for task in tasks[:max_tasks]:
        overdue = f" ⚠️ {task.days_overdue}d overdue" if task.is_overdue else ""
        lines.append(
            f"- [{task.section_name}] {task.content} ({task.project_name}){overdue}"
        )
    return "\n".join(lines)


def _format_habits_for_prompt(habits: list[Task]) -> str:
    if not habits:
        return "No habits tracked."
    lines = []
    for habit in habits:
        last_log = _last_log_line(habit.description)
        lines.append(f"- {habit.content}: {last_log}")
    return "\n".join(lines)


def _last_log_line(description: str) -> str:
    lines = [l.strip() for l in description.splitlines() if l.strip()]
    return lines[-1] if lines else "no log yet"


def _format_waiting_for_prompt(tasks: list[Task]) -> str:
    if not tasks:
        return "Nothing waiting on others."
    count = len(tasks)
    oldest = max(tasks, key=lambda t: t.days_overdue)
    oldest_note = ""
    if oldest.days_overdue > 0:
        oldest_note = f" (oldest: '{oldest.content}', {oldest.days_overdue}d overdue)"
    elif count == 1:
        oldest_note = f": '{oldest.content}'"
    return f"{count} item{'s' if count > 1 else ''} waiting on others{oldest_note}."


def main() -> None:
    """CLI entry point, called by scripts/run_briefing.sh"""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    config = load_config()

    todoist = todoist_from_env()
    memory = MemoryStore(config["memory"]["db_path"])
    claude = ClaudeClient.from_env(config)
    telegram = TelegramClient.from_env(config)

    try:
        run_briefing(todoist, memory, claude, telegram, config)
    except Exception as e:
        logger.error(f"Briefing failed: {e}", exc_info=True)
        try:
            telegram.send_error(f"Briefing failed: {e}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
