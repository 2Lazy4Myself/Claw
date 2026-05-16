"""
briefing.py

Responsibility: Orchestrate the morning briefing flow.

This module is the entry point for the morning cron job. It coordinates the
other modules but contains no business logic of its own. If you find yourself
writing task-filtering logic here, it belongs in todoist_client.py. If you're
building a prompt string here, it belongs in prompts.py.

Flow:
    1. Load config
    2. Fetch today's tasks from Todoist
    3. Fetch recent memory context
    4. Ask Claude to write the briefing
    5. Send via Telegram
    6. Log the session
"""

from __future__ import annotations
import logging
import sys

from claw.claude_client import ClaudeClient
from claw.config import load_config
from claw.memory import MemoryStore, SessionRecord, build_context_block
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, from_env as todoist_from_env
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

    Args:
        todoist: Configured Todoist client.
        memory: Configured memory store.
        claude: Configured Claude client.
        telegram: Configured Telegram client.
        config: Loaded config dict.
    """
    logger.info("Starting morning briefing")

    # 1. Fetch tasks
    tasks = todoist.get_today_tasks(
        include_project_ids=config["todoist"]["include_project_ids"],
        exclude_labels=config["todoist"]["exclude_labels"],
    )
    logger.info(f"Fetched {len(tasks)} tasks from Todoist")

    if not tasks:
        logger.info("No tasks today — sending light all-clear")
        # TODO: send a brief "nothing on today" message
        return

    # 2. Fetch memory context
    recent_sessions = memory.get_recent_sessions(
        n=config["memory"]["recent_sessions_to_include"]
    )
    memory_context = build_context_block(None, recent_sessions)

    # 3. Build task list string for prompt
    task_list = _format_tasks_for_prompt(tasks, config["behaviour"]["briefing_max_tasks"])

    # 4. Ask Claude for the briefing
    system_prompt = prompts.get_prompt("BRIEFING_SYSTEM")
    user_message = prompts.BRIEFING_USER_TEMPLATE.format(
        task_list=task_list,
        memory_context=memory_context,
    )
    briefing_text = claude.complete(
        system=system_prompt,
        user=user_message,
        max_tokens=config["claude"]["briefing_max_tokens"],
    )
    logger.info("Received briefing from Claude")

    # 5. Send via Telegram
    telegram.send_message(briefing_text)
    logger.info("Briefing sent")

    # 6. Log the session
    # TODO: log SessionRecord


def _format_tasks_for_prompt(tasks, max_tasks: int) -> str:
    """
    Converts a list of Task objects to a plain-text block for prompt injection.
    Caps at max_tasks to avoid overwhelming the prompt.

    Pure function — no I/O. Tested in unit tests.
    """
    raise NotImplementedError("Phase 1 implementation")


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
        # Try to send error alert via Telegram
        try:
            telegram.send_error(f"Briefing failed: {e}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
