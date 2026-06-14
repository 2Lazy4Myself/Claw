"""
detectors.py

Responsibility: Post-conversation actions driven by Claude JSON detections.

After a probe conversation ends, these functions ask Claude (cheap model) to read
the transcript and decide whether something concrete happened — a task completed,
a deferral requested, a goal measurement mentioned, a habit to log — then write the
result back to Todoist/Telegram.

Extracted from probe.py. This module is a leaf: probe.py calls into it; it never
calls back. Each detector fails gracefully (logs and returns) on a parse miss —
these actions are optional, so a miss skips the action rather than aborting a probe.
"""

from __future__ import annotations
import json
import logging
from datetime import datetime, date as _date, time as _time, timezone
from typing import Optional

from claw import fitness as fitness_mod
from claw.claude_client import ClaudeClient
from claw.goals import GoalRecord, goal_for_task
from claw.memory import MemoryStore
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, Task
from claw.trajectory import parse_measurement
from claw import prompts

logger = logging.getLogger(__name__)


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
    if outcome in ("no_reply", "timed_out"):
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
        max_tokens=400,
        model=config["claude"]["selection_model"],
    )
    detection = prompts.parse_json_or_none(raw, "Completion detection")
    if detection is None:
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
    if outcome in ("no_reply", "timed_out"):
        return None

    today = _date.today().isoformat()
    raw = claude.complete(
        system=prompts.get_prompt("SNOOZE_DETECTION_SYSTEM"),
        user=(
            f"Today's date: {today}\n"
            f"Task: {task.content}\n\n"
            f"Conversation:\n{json.dumps(history)}"
        ),
        max_tokens=400,
        model=config["claude"]["selection_model"],
    )
    detection = prompts.parse_json_or_none(raw, "Snooze detection")
    if detection is None:
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


def _detect_and_update_goal(
    task: Task,
    goals: list[GoalRecord],
    history: list[dict],
    outcome: str,
    todoist: TodoistClient,
    telegram: TelegramClient,
    claude: ClaudeClient,
    config: dict,
    memory: Optional[MemoryStore] = None,
) -> None:
    """
    After a probe, detects if the user mentioned a concrete measurement update
    for a linked goal's Current field. If so, writes it back to Todoist and
    sends a brief confirmation.
    """
    if outcome in ("no_reply", "timed_out"):
        return

    goal = goal_for_task(task, goals)
    if goal is None or not goal.target:
        return

    raw = claude.complete(
        system=prompts.get_prompt("GOAL_UPDATE_DETECTION_SYSTEM"),
        user=(
            f"Goal: {goal.name}\n"
            f"Target: {goal.target}\n"
            f"Current: {goal.current or 'unknown'}\n\n"
            f"Conversation:\n{json.dumps(history)}"
        ),
        max_tokens=400,
        model=config["claude"]["selection_model"],
    )

    detection = prompts.parse_json_or_none(raw, "Goal update detection")
    if detection is None:
        return

    if not detection.get("updated"):
        return

    value = detection.get("value")
    if not value:
        return

    try:
        todoist.update_goal_current(goal.task_id, value)
        # Record the dated measurement so trajectory/trend can be computed (F1).
        if memory is not None:
            memory.add_goal_measurement(goal.task_id, str(value), parse_measurement(str(value)))
        telegram.send_message(f"Updated: {goal.name} now {value} (target {goal.target}).")
        logger.info(f"Updated goal {goal.task_id} Current: {value}")
    except Exception as e:
        logger.warning(f"Failed to update goal current: {e}")


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


def _append_fitness_programme_log(
    programme,
    task: Task,
    today_session,
    outcome: str,
    todoist: TodoistClient,
) -> None:
    """Appends a structured log entry to the programme task description."""
    today = _date.today()
    day_abbr = today.strftime("%a")
    week_label = f"W{programme.current_week}"
    session_label = (
        today_session.session_type
        if today_session and today_session.session_type
        else task.content
    )
    symbol = "✓" if outcome == "closed" else "—"
    entry = f"[{today.isoformat()} {day_abbr} {week_label}] {symbol} {session_label}"
    fitness_mod.append_programme_log(programme, entry, todoist)
    programme.log_lines.append(entry)  # keep in-memory state consistent for multi-chain sessions


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
        max_tokens=400,
        model=config["claude"]["selection_model"],
    )
    parsed = prompts.parse_json_or_none(raw, "Habit log")
    if parsed is None:
        return

    log_text = parsed.get("log")
    if not log_text:
        logger.warning(f"Habit log response missing 'log' key: {raw!r}")
        return

    today = _date.today().strftime("%-d %b")
    entry = f"\n[{today}] {log_text}"
    if parsed.get("note"):
        entry += f" — {parsed['note']}"

    new_desc = (task.description or "").rstrip() + entry
    try:
        todoist.update_task_description(task.id, new_desc)
        logger.info(f"Appended habit log to task {task.id}: {entry.strip()}")
    except Exception as e:
        logger.warning(f"Failed to write habit log to Todoist: {e}")
