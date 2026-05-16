"""
probe.py

Responsibility: Orchestrate a probe conversation about one stuck task.

Flow:
    1. Load config
    2. Fetch today's tasks
    3. Fetch memory context for each task
    4. Ask Claude to select one task to probe
    5. If no task selected, exit (or send "all clear" if configured)
    6. Build and send the probe opening message
    7. Wait for user reply (with timeout)
    8. If reply received, continue the conversation (up to max_turns)
    9. Log the session outcome
"""

from __future__ import annotations
import json
import logging
import sys
from typing import Optional

from claw.claude_client import ClaudeClient
from claw.config import load_config
from claw.memory import MemoryStore, TaskMemory, SessionRecord, build_context_block
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, Task, from_env as todoist_from_env
from claw import prompts

logger = logging.getLogger(__name__)

MAX_PROBE_TURNS = 4  # Maximum back-and-forth before closing the conversation


def run_probe(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    """
    Runs one complete probe cycle.

    All dependencies are injected for testability.
    """
    logger.info("Starting probe run")

    # 1. Fetch tasks
    tasks = todoist.get_today_tasks(
        include_project_ids=config["todoist"]["include_project_ids"],
        exclude_labels=config["todoist"]["exclude_labels"],
    )

    if not tasks:
        logger.info("No tasks — skipping probe")
        return

    # 2. Select a task
    selected_task = _select_task(tasks, memory, claude, config)
    if selected_task is None:
        logger.info("Claude selected no task to probe — skipping")
        if not config["behaviour"]["skip_probe_if_nothing_to_probe"]:
            telegram.send_message("Nothing particular on my mind today. You're on top of it.")
        return

    logger.info(f"Probing task: {selected_task.display_name}")

    # 3. Open the probe conversation
    task_memory = memory.get_task_memory(selected_task.id)
    recent_sessions = memory.get_recent_sessions(n=3)
    engagement_context = build_context_block(None, recent_sessions)

    system = prompts.get_prompt("PROBE_SYSTEM")
    user_message = prompts.PROBE_USER_TEMPLATE.format(
        task=_format_task_for_prompt(selected_task),
        task_memory=_format_task_memory(task_memory),
        engagement_context=engagement_context,
    )
    opening = claude.complete(
        system=system,
        user=user_message,
        max_tokens=config["claude"]["probe_max_tokens"],
    )
    telegram.send_message(opening)

    # 4. Conversation loop
    conversation_history = [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": opening},
    ]
    outcome = _run_conversation_loop(
        selected_task, conversation_history, memory, claude, telegram, config
    )

    # 5. Log the session
    # TODO: create and log SessionRecord with outcome


def _select_task(
    tasks: list[Task],
    memory: MemoryStore,
    claude: ClaudeClient,
    config: dict,
) -> Optional[Task]:
    """
    Asks Claude to pick one task to probe. Returns the Task, or None.

    The Claude response is JSON — parsed here, not in claude_client.
    """
    raise NotImplementedError("Phase 1 implementation")


def _run_conversation_loop(
    task: Task,
    history: list[dict],
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> str:
    """
    Handles the back-and-forth after the opening message.

    Returns an outcome string: "rescheduled" | "user_committed" | "dropped" |
    "no_reply" | "max_turns_reached" | "closed"
    """
    raise NotImplementedError("Phase 1 implementation")


def _format_task_for_prompt(task: Task) -> str:
    """Formats a Task for prompt injection. Pure function."""
    raise NotImplementedError("Phase 1 implementation")


def _format_task_memory(task_memory) -> str:
    """Formats TaskMemory (or None) for prompt injection. Pure function."""
    if task_memory is None:
        return "No previous history for this task."
    raise NotImplementedError("Phase 1 implementation")


def _format_task_for_selection(task: Task, task_memory) -> str:
    """
    Formats a task + its memory for the task selection prompt.
    Used when asking Claude to choose which task to probe.
    Pure function.
    """
    raise NotImplementedError("Phase 1 implementation")


def main() -> None:
    """CLI entry point, called by scripts/run_probe.sh"""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    config = load_config()

    todoist = todoist_from_env()
    memory = MemoryStore(config["memory"]["db_path"])
    claude = ClaudeClient.from_env(config)
    telegram = TelegramClient.from_env(config)

    try:
        run_probe(todoist, memory, claude, telegram, config)
    except Exception as e:
        logger.error(f"Probe failed: {e}", exc_info=True)
        try:
            telegram.send_error(f"Probe failed: {e}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
