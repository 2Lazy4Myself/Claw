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
import os
import sys
import uuid
from datetime import datetime, date as _date, time as _time, timezone
from typing import Optional

PROBE_LOCK_FILE = "/tmp/claw_probe.lock"

from claw.claude_client import ClaudeClient
from claw.config import load_config
from claw.memory import MemoryStore, TaskMemory, SessionRecord, build_context_block
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, Task, from_env as todoist_from_env
from claw import prompts

logger = logging.getLogger(__name__)

MAX_PROBE_TURNS = 4


_strip_json_fences = prompts.strip_json_fences


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

    # Signal to the listener that the probe owns Telegram polling right now
    try:
        with open(PROBE_LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        logger.warning(f"Could not create probe lock file: {e}")

    try:
        _run_probe_inner(todoist, memory, claude, telegram, config)
    finally:
        try:
            os.unlink(PROBE_LOCK_FILE)
        except FileNotFoundError:
            pass


def _run_probe_inner(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    # 1. Fetch tasks from all configured projects + lifestyle habits + waiting-for
    all_tasks: list[Task] = []
    for project_key in config["todoist"]["projects"]:
        all_tasks.extend(todoist.get_today_and_overdue(project_key))
    all_tasks.extend(todoist.get_lifestyle_habits())
    for project_key in config["todoist"]["projects"]:
        all_tasks.extend(todoist.get_waiting_for(project_key))

    if not all_tasks:
        logger.info("No tasks — skipping probe")
        return

    # 2. Filter: skip tasks probed too recently, or currently snoozed
    # Waiting tasks use a longer staleness threshold (default 72h vs 48h for regular tasks)
    min_hours = config["behaviour"]["min_hours_between_same_task_probe"]
    waiting_min_hours = config["behaviour"].get("waiting_for_min_probe_hours", 72)

    regular_ids = [t.id for t in all_tasks if not t.is_waiting]
    waiting_ids = [t.id for t in all_tasks if t.is_waiting]

    eligible_regular = set(memory.get_tasks_not_recently_probed(regular_ids, min_hours=min_hours))
    eligible_waiting = set(memory.get_tasks_not_recently_probed(waiting_ids, min_hours=waiting_min_hours))
    eligible_ids = eligible_regular | eligible_waiting

    now = datetime.now(timezone.utc)
    base_eligible = [
        t for t in all_tasks
        if t.id in eligible_ids and not _is_snoozed(t, memory, now)
    ]

    if not base_eligible:
        logger.info("All tasks probed recently or snoozed — skipping")
        return

    # 3. Constant Cleaning loop — probe tasks until no engagement or cap hit
    max_chain = config["behaviour"].get("max_chain_length", 5)
    discussed_ids: set[str] = set()
    last_discussed: Optional[Task] = None

    for chain_index in range(max_chain):
        eligible_tasks = [t for t in base_eligible if t.id not in discussed_ids]
        if not eligible_tasks:
            logger.info("No more eligible tasks for this session")
            break

        selected_task = _select_task(eligible_tasks, memory, claude, config, last_discussed=last_discussed)
        if selected_task is None:
            logger.info("Claude selected no task to probe")
            if chain_index == 0 and not config["behaviour"]["skip_probe_if_nothing_to_probe"]:
                telegram.send_message("Nothing particular on my mind today. You're on top of it.")
            break

        logger.info(f"Probing task [{chain_index + 1}/{max_chain}]: {selected_task.display_name}")
        outcome = _probe_one_task(
            selected_task, todoist, memory, claude, telegram, config,
            chain_index=chain_index, last_discussed=last_discussed,
        )

        discussed_ids.add(selected_task.id)
        last_discussed = selected_task

        if outcome == "no_reply":
            logger.info("No reply — ending session")
            break


def _probe_one_task(
    task: Task,
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
    chain_index: int,
    last_discussed: Optional[Task],
) -> str:
    """
    Runs one complete probe conversation for a single task.
    Returns the outcome: "no_reply" | "closed" | "max_turns_reached"
    """
    started_at = datetime.now(timezone.utc)
    task_memory = memory.get_task_memory(task.id)
    recent_sessions = memory.get_recent_sessions(n=3)
    engagement_context = build_context_block(None, recent_sessions)

    chain_context = ""
    if chain_index > 0 and last_discussed is not None:
        chain_context = (
            f"You've already discussed '{last_discussed.content}' tonight. "
            "Move to this next — no recap, just open naturally."
        )

    opening_user_msg = prompts.PROBE_USER_TEMPLATE.format(
        task=_format_task_for_prompt(task),
        task_memory=_format_task_memory(task_memory),
        engagement_context=engagement_context,
        chain_context=chain_context,
    )
    opening = claude.complete(
        system=prompts.get_prompt("PROBE_SYSTEM"),
        user=opening_user_msg,
        max_tokens=config["claude"]["probe_max_tokens"],
    )
    telegram.send_message(opening)

    conversation_history = [
        {"role": "user", "content": opening_user_msg},
        {"role": "assistant", "content": opening},
    ]
    outcome = _run_conversation_loop(task, conversation_history, memory, claude, telegram, config)
    logger.info(f"Probe outcome: {outcome}")

    if task.is_habit:
        _write_habit_log(task, conversation_history, outcome, todoist, claude, config)

    subtasks = todoist.get_subtasks(task.id)
    _detect_and_close(task, subtasks, conversation_history, outcome, todoist, telegram, claude, config)
    snooze_until = _detect_and_snooze(task, conversation_history, outcome, telegram, claude, config)

    raw_transcript = json.dumps(conversation_history)
    summary = _summarise_session(raw_transcript, task, outcome, claude, config)

    memory.log_session(SessionRecord(
        session_id=str(uuid.uuid4()),
        session_type="probe",
        started_at=started_at,
        task_id=task.id,
        engagement_signal=None,
        summary=summary,
        raw_transcript=raw_transcript,
    ))

    existing = task_memory or TaskMemory(
        task_id=task.id,
        last_probed_at=None,
        probe_count=0,
        last_outcome=None,
        notes="",
        snoozed_until=None,
    )
    notes_append = f"\n[{datetime.now(timezone.utc).date()}] {summary}" if summary else ""
    memory.upsert_task_memory(TaskMemory(
        task_id=task.id,
        last_probed_at=datetime.now(timezone.utc),
        probe_count=existing.probe_count + 1,
        last_outcome=outcome,
        notes=(existing.notes + notes_append).strip(),
        snoozed_until=snooze_until or existing.snoozed_until,
    ))

    return outcome


# ─── Task selection ───────────────────────────────────────────────────────────

def _select_task(
    tasks: list[Task],
    memory: MemoryStore,
    claude: ClaudeClient,
    config: dict,
    last_discussed: Optional[Task] = None,
) -> Optional[Task]:
    """
    Asks Claude (cheap model) to pick one task to probe. Returns the Task or None.
    """
    task_list_with_memory = "\n".join(
        _format_task_for_selection(t, memory.get_task_memory(t.id))
        for t in tasks
    )
    previous_topic = last_discussed.content if last_discussed else ""
    raw = claude.complete(
        system=prompts.get_prompt("TASK_SELECTION_SYSTEM"),
        user=prompts.TASK_SELECTION_USER_TEMPLATE.format(
            task_list_with_memory=task_list_with_memory,
            previous_topic=previous_topic,
        ),
        max_tokens=config["claude"]["selection_max_tokens"],
        model=config["claude"]["selection_model"],
    )

    try:
        parsed = json.loads(_strip_json_fences(raw))
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
            system=prompts.get_prompt("SESSION_SUMMARY_SYSTEM"),
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
    lines = []
    if task.is_habit:
        lines.append("Type: LIFESTYLE HABIT")
    elif task.is_waiting:
        lines.append("Type: WAITING FOR")
    lines += [
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
    habit_tag = " [HABIT]" if task.is_habit else (" [WAITING]" if task.is_waiting else "")
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
        f"- task_id: {task.id},{habit_tag} [{task.section_name}] {task.content} "
        f"({task.project_name}){overdue}{memory_str}{snoozed}"
    )


def _detect_and_close(
    task: Task,
    subtasks: list[Task],
    history: list[dict],
    outcome: str,
    todoist: TodoistClient,
    telegram: TelegramClient,
    claude: ClaudeClient,
    config: dict,
) -> None:
    """
    After a probe conversation, asks Claude whether the user indicated completion.
    If so, closes the task or subtask in Todoist and sends a confirmation to Telegram.
    """
    if outcome == "no_reply":
        return

    subtask_names = [s.content for s in subtasks]
    raw = claude.complete(
        system=prompts.get_prompt("COMPLETION_DETECTION_SYSTEM"),
        user=(
            f"Task: {task.content}\n"
            f"Is habit: {task.is_habit}\n"
            f"Known subtasks: {subtask_names if subtask_names else 'none'}\n\n"
            f"Conversation:\n{json.dumps(history)}"
        ),
        max_tokens=80,
        model=config["claude"]["selection_model"],
    )
    try:
        detection = json.loads(_strip_json_fences(raw))
    except json.JSONDecodeError:
        logger.warning(f"Completion detection returned non-JSON: {raw!r}")
        return

    action = detection.get("action", "none")

    if action == "close_task" and not task.is_habit:
        try:
            todoist.close_task(task.id)
            telegram.send_message("✓ Checked off in Todoist.")
            logger.info(f"Closed task {task.id} in Todoist")
        except Exception as e:
            logger.warning(f"Failed to close task: {e}")

    elif action == "close_subtask":
        subtask = _find_subtask(detection.get("subtask_name", ""), subtasks)
        if subtask:
            try:
                todoist.close_task(subtask.id)
                telegram.send_message(f"✓ Checked off '{subtask.content}' in Todoist.")
                logger.info(f"Closed subtask {subtask.id} ({subtask.content})")
            except Exception as e:
                logger.warning(f"Failed to close subtask: {e}")


def _detect_and_snooze(
    task: Task,
    history: list[dict],
    outcome: str,
    telegram: TelegramClient,
    claude: ClaudeClient,
    config: dict,
) -> Optional[datetime]:
    """
    After a probe, detects if the user asked to defer this task.
    Returns the snooze datetime if set, None otherwise.
    Sends a confirmation message via Telegram when a snooze is applied.
    """
    if outcome == "no_reply":
        return None

    today = _date.today().isoformat()
    raw = claude.complete(
        system=prompts.get_prompt("SNOOZE_DETECTION_SYSTEM"),
        user=(
            f"Today's date: {today}\n"
            f"Task: {task.content}\n\n"
            f"Conversation:\n{json.dumps(history)}"
        ),
        max_tokens=80,
        model=config["claude"]["selection_model"],
    )
    try:
        detection = json.loads(_strip_json_fences(raw))
    except json.JSONDecodeError:
        logger.warning(f"Snooze detection returned non-JSON: {raw!r}")
        return None

    if not detection.get("snooze"):
        return None

    date_iso = detection.get("date_iso")
    if not date_iso:
        return None

    try:
        snooze_dt = datetime.combine(
            _date.fromisoformat(date_iso), _time.min, tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        logger.warning(f"Snooze detection returned invalid date: {date_iso!r}")
        return None

    snooze_display = snooze_dt.strftime("%-d %b")
    telegram.send_message(f"Got it — I'll leave this one until {snooze_display}.")
    logger.info(f"Snoozed task {task.id} until {date_iso}")
    return snooze_dt


def _is_snoozed(task: Task, memory: MemoryStore, now: datetime) -> bool:
    """Returns True if this task has an active snooze that hasn't expired."""
    task_mem = memory.get_task_memory(task.id)
    if task_mem is None or task_mem.snoozed_until is None:
        return False
    snooze_dt = task_mem.snoozed_until
    if snooze_dt.tzinfo is None:
        snooze_dt = snooze_dt.replace(tzinfo=timezone.utc)
    return snooze_dt > now


def _find_subtask(name: str, subtasks: list[Task]) -> Optional[Task]:
    """
    Finds a subtask by name. Case-insensitive exact match first, partial match fallback.
    Pure function — used by _detect_and_close and testable independently.
    """
    name_lower = name.strip().lower()
    for s in subtasks:
        if s.content.strip().lower() == name_lower:
            return s
    for s in subtasks:
        if name_lower in s.content.strip().lower():
            return s
    return None


def _write_habit_log(
    task: Task,
    history: list[dict],
    outcome: str,
    todoist: TodoistClient,
    claude: ClaudeClient,
    config: dict,
) -> None:
    """
    Appends a timestamped log entry to the Todoist task description after a habit probe.
    Always writes a one-liner; adds an extended note only when something meaningful happened.
    """
    raw = claude.complete(
        system=prompts.get_prompt("HABIT_LOG_SYSTEM"),
        user=f"Habit: {task.content}\nOutcome: {outcome}\n\n{json.dumps(history)}",
        max_tokens=120,
        model=config["claude"]["selection_model"],
    )
    try:
        parsed = json.loads(_strip_json_fences(raw))
    except json.JSONDecodeError:
        logger.warning(f"Habit log returned non-JSON: {raw!r}")
        return

    today = _date.today().strftime("%-d %b")
    entry = f"\n[{today}] {parsed['log']}"
    if parsed.get("note"):
        entry += f" — {parsed['note']}"

    new_desc = (task.description or "").rstrip() + entry
    try:
        todoist.update_task_description(task.id, new_desc)
        logger.info(f"Appended habit log to task {task.id}: {entry.strip()}")
    except Exception as e:
        logger.warning(f"Failed to write habit log to Todoist: {e}")


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
