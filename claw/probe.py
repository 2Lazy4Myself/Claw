"""
probe.py

Responsibility: Orchestrate a probe conversation about one stuck task.

Flow:
    1. Load config
    2. Fetch today's tasks from all configured projects
    3. Ask Claude (cheap model) to select one task to probe
    4. If no task selected, exit (or send "all clear" if configured)
    5. Open the probe conversation with Claude (Sonnet)
    6. Wait for user reply; continue conversation up to MAX_PROBE_TURNS
    7. Log session, update task memory, summarise with cheap model
"""

from __future__ import annotations
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

from claw.claude_client import ClaudeClient
from claw.config import load_config
from claw.memory import MemoryStore, TaskMemory, SessionRecord, build_context_block
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, Task, from_env as todoist_from_env
from claw import prompts

logger = logging.getLogger(__name__)

MAX_PROBE_TURNS = 4


def run_probe(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    """
    Runs one complete probe cycle. All dependencies injected for testability.
    """
    logger.info("Starting probe run")
    started_at = datetime.now(timezone.utc)

    # 1. Fetch tasks from all configured projects
    all_tasks: list[Task] = []
    for project_key in config["todoist"]["projects"]:
        all_tasks.extend(todoist.get_today_and_overdue(project_key))

    if not all_tasks:
        logger.info("No tasks — skipping probe")
        return

    # 2. Filter: skip tasks probed too recently
    min_hours = config["behaviour"]["min_hours_between_same_task_probe"]
    eligible_ids = memory.get_tasks_not_recently_probed(
        [t.id for t in all_tasks], min_hours=min_hours
    )
    # Also include tasks with no memory record (never probed)
    eligible_tasks = [t for t in all_tasks if t.id in eligible_ids or
                      memory.get_task_memory(t.id) is None]

    if not eligible_tasks:
        logger.info("All tasks probed recently — skipping")
        return

    # 3. Select a task to probe (cheap model)
    selected_task = _select_task(eligible_tasks, memory, claude, config)
    if selected_task is None:
        logger.info("Claude selected no task to probe")
        if not config["behaviour"]["skip_probe_if_nothing_to_probe"]:
            telegram.send_message("Nothing particular on my mind today. You're on top of it.")
        return

    logger.info(f"Probing task: {selected_task.display_name}")

    # 4. Open the probe conversation (Sonnet)
    task_memory = memory.get_task_memory(selected_task.id)
    recent_sessions = memory.get_recent_sessions(n=3)
    engagement_context = build_context_block(None, recent_sessions)

    opening_user_msg = prompts.PROBE_USER_TEMPLATE.format(
        task=_format_task_for_prompt(selected_task),
        task_memory=_format_task_memory(task_memory),
        engagement_context=engagement_context,
    )
    opening = claude.complete(
        system=prompts.get_prompt("PROBE_SYSTEM"),
        user=opening_user_msg,
        max_tokens=config["claude"]["probe_max_tokens"],
    )
    telegram.send_message(opening)

    # 5. Conversation loop
    conversation_history = [
        {"role": "user", "content": opening_user_msg},
        {"role": "assistant", "content": opening},
    ]
    outcome = _run_conversation_loop(
        selected_task, conversation_history, memory, claude, telegram, config
    )
    logger.info(f"Probe outcome: {outcome}")

    # 6. Log session + update task memory
    raw_transcript = json.dumps(conversation_history)

    # Summarise with cheap model
    summary = _summarise_session(raw_transcript, selected_task, outcome, claude, config)

    memory.log_session(SessionRecord(
        session_id=str(uuid.uuid4()),
        session_type="probe",
        started_at=started_at,
        task_id=selected_task.id,
        engagement_signal=None,
        summary=summary,
        raw_transcript=raw_transcript,
    ))

    existing = task_memory or TaskMemory(
        task_id=selected_task.id,
        last_probed_at=None,
        probe_count=0,
        last_outcome=None,
        notes="",
        snoozed_until=None,
    )
    notes_append = f"\n[{datetime.now(timezone.utc).date()}] {summary}" if summary else ""
    memory.upsert_task_memory(TaskMemory(
        task_id=selected_task.id,
        last_probed_at=datetime.now(timezone.utc),
        probe_count=existing.probe_count + 1,
        last_outcome=outcome,
        notes=(existing.notes + notes_append).strip(),
        snoozed_until=existing.snoozed_until,
    ))


# ─── Task selection ───────────────────────────────────────────────────────────

def _select_task(
    tasks: list[Task],
    memory: MemoryStore,
    claude: ClaudeClient,
    config: dict,
) -> Optional[Task]:
    """
    Asks Claude (cheap model) to pick one task to probe. Returns the Task or None.
    """
    task_list_with_memory = "\n".join(
        _format_task_for_selection(t, memory.get_task_memory(t.id))
        for t in tasks
    )
    raw = claude.complete(
        system=prompts.get_prompt("TASK_SELECTION_SYSTEM"),
        user=prompts.TASK_SELECTION_USER_TEMPLATE.format(
            task_list_with_memory=task_list_with_memory
        ),
        max_tokens=config["claude"]["selection_max_tokens"],
        model=config["claude"]["selection_model"],
    )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"Task selection returned non-JSON: {raw!r}")
        return None

    selected_id = parsed.get("task_id")
    if not selected_id:
        logger.info(f"Task selection: no probe needed — {parsed.get('reason', '')}")
        return None

    task_map = {t.id: t for t in tasks}
    if selected_id not in task_map:
        logger.warning(f"Task selection returned unknown id: {selected_id!r}")
        return None

    logger.info(f"Selected task {selected_id}: {parsed.get('reason', '')}")
    return task_map[selected_id]


# ─── Conversation loop ────────────────────────────────────────────────────────

def _run_conversation_loop(
    task: Task,
    history: list[dict],
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> str:
    """
    Handles back-and-forth after the opening message.

    Returns: "no_reply" | "closed" | "max_turns_reached"
    """
    timeout = config["telegram"]["reply_timeout_seconds"]

    for turn in range(MAX_PROBE_TURNS):
        reply = telegram.wait_for_reply(timeout)
        if reply is None:
            return "no_reply"

        history.append({"role": "user", "content": reply})

        followup = claude.complete_with_history(
            system=prompts.get_prompt("PROBE_FOLLOWUP_SYSTEM"),
            messages=history,
            max_tokens=config["claude"]["probe_max_tokens"],
        )
        telegram.send_message(followup)
        history.append({"role": "assistant", "content": followup})

        if _is_conversation_closed(followup):
            return "closed"

    return "max_turns_reached"


def _is_conversation_closed(response: str) -> bool:
    """
    Heuristic: a short response with no question is treated as a natural close.
    Extension seam — Phase 4 can swap this for a JSON call without touching the loop.
    """
    return not response.rstrip().endswith("?") and len(response.split()) < 60


# ─── Session summarisation ────────────────────────────────────────────────────

def _summarise_session(
    transcript: str,
    task: Task,
    outcome: str,
    claude: ClaudeClient,
    config: dict,
) -> Optional[str]:
    """
    Asks Claude (cheap model) to write a 1-2 sentence summary of what happened.
    Stored in SessionRecord.summary and appended to TaskMemory.notes.
    """
    try:
        return claude.complete(
            system=(
                "Summarise this probe conversation in 1-2 sentences. "
                "Focus on what was said, the outcome, and any commitment made. "
                "Be factual and brief. No fluff."
            ),
            user=f"Task: {task.content}\nOutcome: {outcome}\n\nTranscript:\n{transcript}",
            max_tokens=100,
            model=config["claude"]["selection_model"],
        )
    except Exception as e:
        logger.warning(f"Failed to summarise session: {e}")
        return None


# ─── Pure format functions ────────────────────────────────────────────────────

def _format_task_for_prompt(task: Task) -> str:
    """Formats a Task for prompt injection."""
    lines = [
        f"Content: {task.content}",
        f"Section: {task.section_name}",
        f"Project: {task.project_name}",
        f"Priority: {task.priority}",
    ]
    if task.description:
        lines.append(f"Description: {task.description}")
    if task.is_overdue:
        lines.append(f"Overdue: {task.days_overdue} day{'s' if task.days_overdue != 1 else ''}")
    if task.labels:
        lines.append(f"Labels: {', '.join(task.labels)}")
    return "\n".join(lines)


def _format_task_memory(task_memory: Optional[TaskMemory]) -> str:
    """Formats TaskMemory for prompt injection."""
    if task_memory is None:
        return "No previous history for this task."
    from claw.memory import _days_ago
    age = _days_ago(task_memory.last_probed_at)
    age_str = f"{age} day{'s' if age != 1 else ''} ago" if age is not None else "unknown"
    parts = [
        f"Last probed: {age_str}",
        f"Probe count: {task_memory.probe_count}",
        f"Last outcome: {task_memory.last_outcome or 'unknown'}",
    ]
    if task_memory.notes:
        parts.append(f"Notes: {task_memory.notes[:300]}")
    if task_memory.snoozed_until:
        parts.append(f"Snoozed until: {task_memory.snoozed_until.date()}")
    return "\n".join(parts)


def _format_task_for_selection(task: Task, task_memory: Optional[TaskMemory]) -> str:
    """
    Compact one-liner for the task selection prompt.
    Claude needs just enough to make a good choice.
    """
    overdue = f", overdue {task.days_overdue}d" if task.is_overdue else ""
    from claw.memory import _days_ago
    if task_memory and task_memory.last_probed_at:
        age = _days_ago(task_memory.last_probed_at)
        memory_str = f", last probed {age}d ago, outcome: {task_memory.last_outcome or 'unknown'}"
    else:
        memory_str = ", never probed"

    snoozed = ""
    if task_memory and task_memory.snoozed_until:
        snoozed = f", SNOOZED until {task_memory.snoozed_until.date()}"

    return (
        f"- task_id: {task.id}, [{task.section_name}] {task.content} "
        f"({task.project_name}){overdue}{memory_str}{snoozed}"
    )


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
