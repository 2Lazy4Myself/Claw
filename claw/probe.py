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
import queue
import sys
import uuid
from datetime import datetime, date as _date, timezone
from typing import Optional

from claw.claude_client import ClaudeClient
from claw.config import load_config
from claw import fitness as fitness_mod
from claw.goals import get_goals, build_goal_summary, goal_line_for_task, GoalRecord
from claw.memory import MemoryStore, TaskMemory, SessionRecord, build_context_block
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, Task, from_env as todoist_from_env
from claw import prompts
from claw.watchlist import get_overdue_topics, OverdueTopic
# Extracted helpers — re-exported here so existing callers (listener, tests) keep
# importing them from claw.probe even though they now live in focused modules.
from claw.selection import (
    _select_task,
    _extract_partial_selection,
    _format_task_for_selection,
)
from claw.detectors import (
    _detect_and_close,
    _detect_and_snooze,
    _detect_and_update_goal,
    _write_habit_log,
    _append_fitness_programme_log,
    _find_subtask,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_PROBE_TURNS = 20  # safety valve — conversations close via inactivity, not this


def run_probe(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
    reply_queue: Optional[queue.Queue] = None,
) -> None:
    """
    Runs one complete probe cycle. All dependencies injected for testability.

    reply_queue: shared queue fed by the daemon's polling thread. When provided,
    wait_for_reply reads from it instead of calling the Telegram API directly.
    """
    logger.info("Starting probe run")
    _run_probe_inner(todoist, memory, claude, telegram, config, reply_queue)


def _run_probe_inner(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
    reply_queue: Optional[queue.Queue] = None,
) -> None:
    cap = config["schedule"].get("max_pending_messages", 3)
    if memory.pending_count() >= cap:
        logger.info(f"Pending message cap ({cap}) reached — skipping probe")
        return

    # 1. Fetch tasks from all configured projects + lifestyle habits + waiting-for
    all_tasks: list[Task] = []
    for project_key in config["todoist"]["projects"]:
        all_tasks.extend(todoist.get_today_and_overdue(project_key))
    habits, goal_tasks = todoist.get_claw_data()
    all_tasks.extend(habits)
    for project_key in config["todoist"]["projects"]:
        all_tasks.extend(todoist.get_waiting_for(project_key))

    # Fitness programme — fetch once and carry through the session
    programme_tasks = todoist.get_programmes()
    active_programme = fitness_mod.get_active_programme(programme_tasks)
    if active_programme:
        compliance = fitness_mod.get_week_compliance(active_programme)
        fitness_urgency = fitness_mod.compliance_urgency(compliance)
    else:
        fitness_urgency = "normal"

    if not all_tasks:
        logger.info("No tasks — skipping probe")
        return

    # Resolve goals + user profile early — needed by both the watchlist
    # check-in path and the selection loop below.
    goals = get_goals(goal_tasks)
    user_profile = memory.get_user_profile()

    # 2. Watchlist check — bypasses the 48h recency filter.
    # If any topic (fitness, goal, habit) has been silent for too long, run a
    # targeted check-in and return; the normal probe fires on the next cron tick.
    overdue = get_overdue_topics(memory, all_tasks, goals, active_programme, config, _date.today())
    if overdue:
        topic = overdue[0]
        logger.info(f"Watchlist check-in: '{topic.topic_name}' silent {topic.days_silent}d")
        _run_checkin(topic, todoist, memory, claude, telegram, config, cap, reply_queue, goals, active_programme, user_profile=user_profile)
        return

    # 3. Filter: skip tasks probed too recently, or currently snoozed
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

    # 4. Constant Cleaning loop — probe tasks until no engagement or cap hit
    goal_context = build_goal_summary(all_tasks, goals, memory)

    max_chain = config["behaviour"].get("max_chain_length", 5)
    discussed_ids: set[str] = set()
    last_discussed: Optional[Task] = None

    for chain_index in range(max_chain):
        eligible_tasks = [t for t in base_eligible if t.id not in discussed_ids]
        if not eligible_tasks:
            logger.info("No more eligible tasks for this session")
            break

        selected_task = _select_task(
            eligible_tasks, memory, claude, config,
            last_discussed=last_discussed, goal_context=goal_context,
            fitness_urgency=fitness_urgency,
        )
        if selected_task is None:
            logger.info("Claude selected no task to probe")
            if chain_index == 0 and not config["behaviour"]["skip_probe_if_nothing_to_probe"]:
                telegram.send_message(prompts.get_prompt("MSG_PROBE_ALL_CLEAR"))
            break

        logger.info(f"Probing task [{chain_index + 1}/{max_chain}]: {selected_task.display_name}")
        outcome = _probe_one_task(
            selected_task, todoist, memory, claude, telegram, config,
            chain_index=chain_index, last_discussed=last_discussed, goals=goals, cap=cap,
            reply_queue=reply_queue, active_programme=active_programme,
            user_profile=user_profile,
        )

        discussed_ids.add(selected_task.id)
        last_discussed = selected_task

        if outcome in ("no_reply", "timed_out"):
            logger.info(f"Session ended ({outcome}) — not chaining")
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
    goals: Optional[list[GoalRecord]] = None,
    cap: int = 3,
    reply_queue: Optional[queue.Queue] = None,
    active_programme=None,
    checkin_ctx: str = "",
    user_profile: Optional[str] = None,
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

    # Detect fitness task first — guards goal_line injection below
    is_fitness = (
        active_programme is not None
        and any(label in task.labels for label in active_programme.labels)
    )

    # Suppress goal_line for fitness tasks: FITNESS_PROBE_SYSTEM is the trainer persona;
    # injecting a weight-goal frame alongside it makes Claude blend topics (the bug that
    # caused mixed questions about strength goals vs. weight targets).
    g_line = "" if is_fitness else goal_line_for_task(task, goals or [], memory)
    goal_line = f"{g_line}\n" if g_line else ""
    if is_fitness:
        today_session = fitness_mod.get_today_session(active_programme, _date.today())
        probe_compliance = fitness_mod.get_week_compliance(active_programme)
        fitness_probe_ctx = fitness_mod.build_fitness_probe_context(
            active_programme, today_session, probe_compliance, task
        )
        system_prompt_name = "FITNESS_PROBE_SYSTEM"
    else:
        today_session = None
        fitness_probe_ctx = ""
        system_prompt_name = "PROBE_SYSTEM"

    checkin_context = (checkin_ctx + "\n") if checkin_ctx else ""
    user_profile_block = f"User profile:\n{user_profile}\n\n" if user_profile else ""
    opening_user_msg = prompts.PROBE_USER_TEMPLATE.format(
        user_profile=user_profile_block,
        checkin_context=checkin_context,
        task=_format_task_for_prompt(task),
        goal_line=goal_line,
        task_memory=_format_task_memory(task_memory, task.id, memory),
        engagement_context=engagement_context,
        chain_context=chain_context,
        fitness_context=fitness_probe_ctx,
    )
    opening = claude.complete(
        system=prompts.get_prompt(system_prompt_name),
        user=opening_user_msg,
        max_tokens=config["claude"]["probe_max_tokens"],
    )

    # Race-guard: cap may have been reached between the outer check and this send
    msg_code = memory.assign_message_code(opening, "probe", cap)
    if msg_code is None:
        logger.info("Pending message cap reached before send — skipping task")
        return "no_reply"

    telegram.send_message(f"{msg_code}: {opening}", buttons=prompts.PROBE_ACTION_BUTTONS)

    conversation_history = [
        {"role": "user", "content": opening_user_msg},
        {"role": "assistant", "content": opening},
    ]
    outcome = _run_conversation_loop(task, conversation_history, memory, claude, telegram, config, reply_queue)
    logger.info(f"Probe outcome: {outcome}")

    # If the user engaged, the slot is answered — close it so the next cron can top up
    if outcome != "no_reply":
        memory.close_message_code(msg_code)
        # Flush the probe's turns into rolling chat memory so a follow-up message
        # after the probe closes (or times out) continues the same thread instead
        # of hitting the stateless general handler cold.
        _flush_probe_turns_to_chat(memory, task, conversation_history)

    if task.is_habit:
        _write_habit_log(task, conversation_history, outcome, todoist, claude, config)
        if is_fitness and active_programme is not None and outcome != "no_reply":
            _append_fitness_programme_log(
                active_programme, task, today_session, outcome, todoist
            )

    subtasks = todoist.get_subtasks(task.id)
    _detect_and_close(task, subtasks, conversation_history, outcome, todoist, telegram, claude, config)
    snooze_until = _detect_and_snooze(task, conversation_history, outcome, telegram, claude, config)
    if goals:
        _detect_and_update_goal(task, goals, conversation_history, outcome, todoist, telegram, claude, config, memory)

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
        context_summary=existing.context_summary,
    ))

    return outcome


# ─── Watchlist check-in ──────────────────────────────────────────────────────

def _run_checkin(
    topic: OverdueTopic,
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
    cap: int,
    reply_queue: Optional[queue.Queue],
    goals: Optional[list[GoalRecord]],
    active_programme=None,
    user_profile: Optional[str] = None,
) -> None:
    """Runs a gap-aware check-in for a topic that has been silent too long."""
    gap_note = _gap_note(topic.days_silent, config)

    if topic.topic_type == "fitness" and active_programme is not None:
        today_session = fitness_mod.get_today_session(active_programme, _date.today())
        probe_compliance = fitness_mod.get_week_compliance(active_programme)
        topic_ctx = fitness_mod.build_fitness_probe_context(
            active_programme, today_session, probe_compliance, topic.task
        )
    elif topic.topic_type == "goal":
        topic_ctx = goal_line_for_task(topic.task, goals or [], memory)
    else:
        topic_ctx = ""

    checkin_ctx = f"{gap_note}\n{topic_ctx}".strip()
    _probe_one_task(
        topic.task, todoist, memory, claude, telegram, config,
        chain_index=0, last_discussed=None, goals=goals, cap=cap,
        reply_queue=reply_queue, active_programme=active_programme,
        checkin_ctx=checkin_ctx,
        user_profile=user_profile,
    )


def _gap_note(days_silent: int, config: dict) -> str:
    urgent = config.get("watchlist", {}).get("urgent_threshold_days", 14)
    if days_silent >= urgent:
        return (
            f"CONTEXT: This topic has been silent for {days_silent} days. "
            "Open with genuine curiosity about what's been happening — "
            "offer concrete paths forward, not just open questions."
        )
    return (
        f"CONTEXT: This topic hasn't come up in {days_silent} days. "
        "Open by gently acknowledging the gap."
    )


# ─── Conversation loop ────────────────────────────────────────────────────────

def _run_conversation_loop(
    task: Task,
    history: list[dict],
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
    reply_queue: Optional[queue.Queue] = None,
) -> str:
    """
    Handles back-and-forth after the opening message.

    Returns: "no_reply" | "closed" | "timed_out" | "max_turns_reached"

    Conversations are open-ended — they continue until Claude closes naturally,
    the user goes quiet (inactivity timeout), or the safety-valve turn cap fires.
    """
    timeout = config["telegram"]["reply_timeout_seconds"]

    max_turns = config["behaviour"].get("max_probe_turns", _DEFAULT_MAX_PROBE_TURNS)

    for turn in range(max_turns):
        # A button tap returns its callback_data ('act:*'); map it to the equivalent
        # reply text so the rest of the loop (and the detectors) treat tap == typing.
        reply = prompts.resolve_action_reply(telegram.wait_for_reply(timeout, reply_queue))

        if reply is None:
            # Inactivity timeout fired.
            if turn == 0:
                return "no_reply"
            # Was in conversation — close gracefully with a contextual message.
            closing = _generate_timeout_close(history, claude, config)
            telegram.send_message(closing)
            return "timed_out"

        history.append({"role": "user", "content": reply})

        followup = claude.complete_with_history(
            system=prompts.get_prompt("PROBE_FOLLOWUP_SYSTEM"),
            messages=history,
            max_tokens=config["claude"]["probe_max_tokens"],
        )
        telegram.send_message(followup, buttons=prompts.PROBE_ACTION_BUTTONS)
        history.append({"role": "assistant", "content": followup})

        if _is_conversation_closed(followup):
            return "closed"

    # Safety valve: max_turns hit (shouldn't happen in normal use).
    # Close cleanly rather than leaving a hanging question.
    closing = _generate_timeout_close(history, claude, config)
    telegram.send_message(closing)
    return "max_turns_reached"


def _flush_probe_turns_to_chat(
    memory: MemoryStore,
    task: Task,
    conversation_history: list[dict],
) -> None:
    """
    Append a probe's conversation into rolling chat memory.

    The first history entry is the large templated opening user message — replace
    it with a compact marker so it doesn't bloat the chat thread; store the rest
    (the assistant opening and the real back-and-forth) verbatim.
    """
    if not conversation_history:
        return
    try:
        memory.append_chat_turn("user", f"[probe: {task.content}]", "probe")
        for turn in conversation_history[1:]:
            memory.append_chat_turn(turn["role"], turn["content"], "probe")
    except Exception as e:
        logger.warning(f"Failed to flush probe turns to chat memory: {e}")


def _generate_timeout_close(
    history: list[dict],
    claude: ClaudeClient,
    config: dict,
) -> str:
    """Generates a contextual closing message when the conversation goes quiet or hits the turn cap."""
    # Pass only the last 6 messages — enough context for a closing line, cheaper than the full history.
    trimmed = history[:2] + history[-6:] if len(history) > 8 else history
    try:
        return claude.complete_with_history(
            system=prompts.get_prompt("PROBE_TIMEOUT_CLOSE_SYSTEM"),
            messages=trimmed,
            max_tokens=120,
            model=config["claude"]["selection_model"],
        )
    except Exception as e:
        logger.warning(f"Failed to generate timeout close: {e}", exc_info=True)
        return prompts.get_prompt("MSG_PROBE_TIMEOUT_FALLBACK")


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
            max_tokens=400,
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


def _format_task_memory(
    task_memory: Optional[TaskMemory],
    task_id: str,
    memory: MemoryStore,
) -> str:
    """Formats TaskMemory for prompt injection, including recent session history."""
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

    # Synthesised context takes priority over raw notes snippet
    if task_memory.context_summary:
        parts.append(f"Context summary:\n{task_memory.context_summary}")
    elif task_memory.notes:
        parts.append(f"Notes: {task_memory.notes[:300]}")

    # Surface actual session summaries (not just a count)
    recent = memory.get_task_sessions(task_id, limit=5)
    history_lines = [
        f"[{s.started_at.strftime('%-d %b')}] {s.summary}"
        for s in recent
        if s.summary
    ]
    if history_lines:
        parts.append("Recent probe history:\n" + "\n".join(history_lines))

    if task_memory.snoozed_until:
        parts.append(f"Snoozed until: {task_memory.snoozed_until.date()}")
    return "\n".join(parts)


def _is_snoozed(task: Task, memory: MemoryStore, now: datetime) -> bool:
    """Returns True if this task has an active snooze that hasn't expired."""
    task_mem = memory.get_task_memory(task.id)
    if task_mem is None or task_mem.snoozed_until is None:
        return False
    snooze_dt = task_mem.snoozed_until
    if snooze_dt.tzinfo is None:
        snooze_dt = snooze_dt.replace(tzinfo=timezone.utc)
    return snooze_dt > now


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
