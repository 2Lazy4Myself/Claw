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
from datetime import datetime, timezone

from claw.claude_client import ClaudeClient
from claw.config import load_config
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

    # 4. Ask Claude for the briefing
    briefing_text = claude.complete(
        system=prompts.get_prompt("BRIEFING_SYSTEM"),
        user=prompts.BRIEFING_USER_TEMPLATE.format(
            task_list=task_list,
            memory_context=memory_context,
        ),
        max_tokens=config["claude"]["briefing_max_tokens"],
    )
    logger.info("Received briefing from Claude")

    # 5. Send via Telegram
    telegram.send_message(briefing_text)
    logger.info("Briefing sent")

    # 6. Log the session
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
