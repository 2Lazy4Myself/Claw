"""
listener.py

Responsibility: Process inbound Telegram messages outside of probe sessions.

Since Phase 5 (the daemon, ADR-008) the primary entry point is handle_update():
the daemon's main loop pulls one update at a time off the shared queue that the
background polling thread fills, and calls handle_update() to route it. There is
no cron cadence and no lock file — the daemon is the single Telegram consumer, so
no mutual exclusion is needed. run_listener() is retained for script/manual use,
where it batch-fetches updates against the persisted offset.

Message routing:
  - M-code reply (regex fast-path) → close pending message code(s), bypassing Claude
  - free-form topic update          → matched against the watchlist, logged, acked
  - "briefing" intent               → run a full morning briefing
  - "probe" intent                  → start an on-demand probe session
  - "general" intent                → Claude responds with session context

Snooze requests during a probe are handled by probe.py. The listener doesn't
manage snooze independently because it lacks the task-conversation context.
"""

from __future__ import annotations
import json
import logging
import queue
import re
import sys
from datetime import date, datetime, timezone
from typing import Optional

from claw.config import load_config
from claw.memory import MemoryStore, TaskMemory
from claw.claude_client import ClaudeClient
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, SECTION_DISPLAY, from_env as todoist_from_env
from claw import prompts
from claw import situation

# Valid capture targets — anything outside these falls back to the configured default.
_CAPTURE_PROJECTS = {"work", "home"}
_CAPTURE_SECTIONS = {"TODAY", "NEXT_FEW", "THIS_WEEK", "NEXT_WEEK", "THIS_MONTH", "UNPROCESSED"}

# Matches any M-code (M1–M9) in a message — used to detect code replies
_CODE_RE = re.compile(r'\b(M\d)\b', re.IGNORECASE)
# Regex fallback for intent classification when JSON is truncated
_INTENT_RE = re.compile(r'"intent"\s*:\s*"(briefing|probe|general)"')

logger = logging.getLogger(__name__)


def handle_update(
    update: dict,
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
    reply_queue: Optional[queue.Queue] = None,
) -> None:
    """
    Process one Telegram update dict. Called by the daemon dispatch loop.
    Filters for valid text messages from the allowed user, then routes.
    """
    allowed_id = config["telegram"]["allowed_user_id"]

    # Button tap (callback_query) — reaches here only when no probe is actively
    # waiting (the live probe consumes taps via wait_for_reply). Acknowledge it and
    # route the mapped reply text through the normal handler so a tap always acts.
    cb = update.get("callback_query")
    if cb:
        if cb.get("from", {}).get("id") != allowed_id:
            return
        uid = update.get("update_id")
        if uid is not None and memory.already_handled(uid):
            logger.info(f"Skipping already-handled update {uid}")
            return
        telegram.answer_callback_query(cb["id"])
        text = prompts.resolve_action_reply(cb.get("data", ""))
        if text:
            logger.info(f"Listener handling button tap: {text!r}")
            _handle_message(text, todoist, memory, claude, telegram, config, reply_queue)
        if uid is not None:
            memory.mark_handled(uid)
        return

    msg = update.get("message")
    if not msg:  # ignore edited_message — edits to prior messages are not new input
        return
    if msg.get("from", {}).get("id") != allowed_id:
        return
    uid = update.get("update_id")
    # Idempotency guard: Telegram delivery is at-least-once, so an update can be
    # redelivered (e.g. a lost ack) after it was already processed. Dropping it
    # here makes reprocessing harmless — notably it stops a probe reply that was
    # consumed in a now-gone session from being re-handled out of context (ADR-014).
    if uid is not None and memory.already_handled(uid):
        logger.info(f"Skipping already-handled update {uid}")
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    logger.info(f"Listener handling message: {text[:60]!r}")
    _handle_message(text, todoist, memory, claude, telegram, config, reply_queue)
    if uid is not None:
        memory.mark_handled(uid)


def run_listener(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    """
    Fetch-and-dispatch loop for script/manual use. Manages its own offset.
    In the daemon, main.py handles polling and calls handle_update() directly.
    """
    offset = memory.get_listener_offset()
    updates = telegram.get_updates(offset=offset, timeout=0)

    if not updates:
        return

    allowed_id = config["telegram"]["allowed_user_id"]
    consumed_offset: Optional[int] = None
    for update in updates:
        consumed_offset = update["update_id"] + 1
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            continue
        if msg.get("from", {}).get("id") != allowed_id:
            continue
        if memory.already_handled(update["update_id"]):  # see ADR-014
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue

        logger.info(f"Listener handling message: {text[:60]!r}")
        _handle_message(text, todoist, memory, claude, telegram, config)
        memory.mark_handled(update["update_id"])
        break  # one message per script run

    if consumed_offset is not None:
        memory.set_listener_offset(consumed_offset)


def _parse_code_replies(text: str) -> list[tuple[str, str]]:
    """
    Extracts all M-code replies from a message.
    "M2 - yeah, M1 - No" → [("M2", "yeah"), ("M1", "No")]
    """
    matches = list(_CODE_RE.finditer(text))
    results = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        reply = text[m.end():end].strip().lstrip("-:–").strip().rstrip(",;").strip()
        results.append((m.group(1).upper(), reply))
    return results


def _handle_message(
    text: str,
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
    reply_queue: Optional[queue.Queue] = None,
) -> None:
    # Fast-path: M-code replies bypass Claude intent classification
    code_replies = _parse_code_replies(text)
    if code_replies:
        _handle_code_replies(code_replies, memory, telegram)
        return

    raw = claude.complete(
        system=prompts.get_prompt("LISTENER_INTENT_SYSTEM"),
        user=text,
        max_tokens=300,
        model=config["claude"]["selection_model"],
    )
    try:
        intent = json.loads(prompts.strip_json_fences(raw)).get("intent", "general")
    except (json.JSONDecodeError, AttributeError):
        m = _INTENT_RE.search(raw)
        intent = m.group(1) if m else "general"

    if intent == "briefing":
        _handle_briefing(todoist, memory, claude, telegram, config)
    elif intent == "probe":
        _handle_probe(todoist, memory, claude, telegram, config, reply_queue)
    elif intent == "capture":
        _handle_capture(text, todoist, claude, telegram, config)
    else:
        # General path: gather the live task/goal/programme snapshot once, shared
        # by the free-form topic matcher and the conversational fallback so one
        # message never triggers two Todoist fetches.
        all_tasks, habits, goals, programme = situation.gather_active_context(todoist, config)
        # First check if it's a free-form topic update. If matched, log it and
        # acknowledge; otherwise fall back to a conversational reply.
        if len(text.strip()) > 10:
            matched = _handle_free_form_update(
                text, all_tasks, habits, goals, programme,
                todoist, memory, claude, telegram, config,
            )
            if matched:
                return
        _handle_general(
            text, all_tasks, habits, goals, programme,
            memory, claude, telegram, config,
        )


def _handle_code_replies(
    code_replies: list[tuple[str, str]],
    memory: MemoryStore,
    telegram: TelegramClient,
) -> None:
    closed: list[str] = []
    unknown: list[str] = []
    for code, _reply in code_replies:
        row = memory.close_message_code(code)
        if row:
            closed.append(code)
            logger.info(f"Closed pending message {code}")
        else:
            unknown.append(code)
            logger.info(f"No pending message found for {code}")

    if closed:
        telegram.send_message(prompts.get_prompt("MSG_CODES_CLOSED").format(codes=", ".join(closed)))
    if unknown:
        telegram.send_message(prompts.get_prompt("MSG_CODES_UNKNOWN").format(codes=", ".join(unknown)))


def _handle_probe(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
    reply_queue: Optional[queue.Queue] = None,
) -> None:
    from claw.probe import run_probe
    try:
        run_probe(todoist, memory, claude, telegram, config, reply_queue)
    except Exception as e:
        logger.error(f"On-demand probe failed: {e}", exc_info=True)
        telegram.send_error(f"Probe failed: {e}")


def _handle_capture(
    text: str,
    todoist: TodoistClient,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    """
    Extracts a task from a capture message and creates it in Todoist.
    The model picks project + time-horizon section; unknown values fall back to
    the configured defaults so a bad classification still lands somewhere sensible.
    """
    behaviour = config.get("behaviour", {})
    raw = claude.complete(
        system=prompts.get_prompt("CAPTURE_EXTRACTION_SYSTEM"),
        user=text,
        max_tokens=200,
        model=config["claude"]["selection_model"],
    )
    parsed = prompts.parse_json_or_none(raw, "Capture extraction")
    if parsed is None:
        telegram.send_message(
            prompts.get_prompt("MSG_TASK_CAPTURE_FAILED").format(error="couldn't read that one")
        )
        return

    content = (parsed.get("content") or "").strip()
    if not content:
        telegram.send_message(
            prompts.get_prompt("MSG_TASK_CAPTURE_FAILED").format(error="no task found in that")
        )
        return

    project = parsed.get("project")
    if project not in _CAPTURE_PROJECTS:
        project = behaviour.get("capture_default_project", "home")
    section = parsed.get("section")
    if section not in _CAPTURE_SECTIONS:
        section = behaviour.get("capture_default_section", "TODAY")

    try:
        todoist.create_task(content, project, section)
    except Exception as e:
        logger.error(f"Task capture failed: {e}", exc_info=True)
        telegram.send_message(prompts.get_prompt("MSG_TASK_CAPTURE_FAILED").format(error=str(e)))
        return

    logger.info(f"Captured task to {project}/{section}: {content!r}")
    telegram.send_message(prompts.get_prompt("MSG_TASK_CAPTURED").format(
        project=project, section=SECTION_DISPLAY.get(section, section), content=content,
    ))


def _handle_briefing(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    from claw.briefing import run_briefing
    try:
        run_briefing(todoist, memory, claude, telegram, config)
    except Exception as e:
        logger.error(f"Listener briefing failed: {e}", exc_info=True)
        telegram.send_error(f"Briefing failed: {e}")


def _pick_topic_for_free_message(
    message: str,
    watchlist_topics: list,
    claude: ClaudeClient,
    config: dict,
) -> object:
    """
    Haiku classification: which watchlist topic does this message update?
    Returns the matching OverdueTopic or None if no high-confidence match.
    """
    if not watchlist_topics:
        return None

    topic_list = "\n".join(
        f"- {t.topic_name} ({t.topic_type})" for t in watchlist_topics
    )
    raw = claude.complete(
        system=prompts.get_prompt("FREE_FORM_TOPIC_DETECTION_SYSTEM"),
        user=f"Tracked topics:\n{topic_list}\n\nUser message: {message}",
        max_tokens=200,
        model=config["claude"]["selection_model"],
    )
    detection = prompts.parse_json_or_none(raw, "Free-form topic detection")
    if detection is None:
        return None

    if not detection.get("matched") or detection.get("confidence") != "high":
        return None

    topic_name = detection.get("topic_name", "")
    for t in watchlist_topics:
        if t.topic_name.lower() == topic_name.lower():
            return t
    return None


def _handle_free_form_update(
    text: str,
    all_tasks: list,
    habits: list,
    goals: list,
    active_programme: object,
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> bool:
    """
    Detects if a free-form message updates a tracked watchlist topic.
    If matched: logs the update, resets the silence clock, sends acknowledgment.
    Returns True if handled, False if no match (caller falls through to general handler).

    The task/goal/programme snapshot is gathered once by the caller and passed in.
    """
    from claw.watchlist import get_overdue_topics

    overdue = get_overdue_topics(
        memory, all_tasks + habits, goals, active_programme, config, date.today()
    )
    if not overdue:
        return False

    matched = _pick_topic_for_free_message(text, overdue, claude, config)
    if matched is None:
        return False

    logger.info(f"Free-form update matched topic: {matched.topic_name}")

    # Synthetic single-message pseudo-transcript for detection prompts
    history = [{"role": "user", "content": text}]

    if matched.topic_type in ("fitness", "habit"):
        from claw.probe import _write_habit_log
        try:
            _write_habit_log(matched.task, history, "max_turns_reached", todoist, claude, config)
        except Exception as e:
            logger.warning(f"Free-form habit log write failed: {e}")
    elif matched.topic_type == "goal":
        from claw.probe import _detect_and_update_goal
        try:
            _detect_and_update_goal(matched.task, goals, history, "max_turns_reached", todoist, telegram, claude, config, memory)
        except Exception as e:
            logger.warning(f"Free-form goal update failed: {e}")

    # Reset the silence clock so the watchlist won't immediately re-trigger
    existing = memory.get_task_memory(matched.task.id)
    memory.upsert_task_memory(TaskMemory(
        task_id=matched.task.id,
        last_probed_at=datetime.now(timezone.utc),
        probe_count=existing.probe_count if existing else 0,
        last_outcome=existing.last_outcome if existing else None,
        notes=existing.notes if existing else "",
        snoozed_until=existing.snoozed_until if existing else None,
    ))

    telegram.send_message(prompts.get_prompt("MSG_FREEFORM_LOGGED").format(topic=matched.topic_name))
    return True


def _handle_general(
    text: str,
    all_tasks: list,
    habits: list,
    goals: list,
    active_programme: object,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    window = config["behaviour"].get("chat_memory_window_minutes", 30)
    max_turns = config["behaviour"].get("chat_memory_max_turns", 12)

    # Rolling short-term memory: recent turns (general chat + flushed probe turns)
    # within the inactivity window form one continuous conversation.
    history = memory.get_recent_chat_turns(window, max_turns)

    # Current-situation snapshot so Claw knows where the user is in their programme.
    snapshot = situation.build_situation_snapshot(
        all_tasks, habits, goals, active_programme, memory, date.today()
    )
    user_content = f"Current situation:\n{snapshot}\n\n{text}" if snapshot else text
    messages = history + [{"role": "user", "content": user_content}]

    try:
        response = claude.complete_with_history(
            system=prompts.get_prompt("LISTENER_GENERAL_SYSTEM"),
            messages=messages,
            max_tokens=200,
        )
        telegram.send_message(response)
    except Exception as e:
        logger.error(f"Listener general response failed: {e}", exc_info=True)
        telegram.send_error(f"Something went wrong: {e}")
        return

    # Persist both sides so the next message continues the thread. Store the raw
    # user text (not the snapshot-wrapped payload) — the snapshot is rebuilt fresh
    # each turn and would otherwise bloat history.
    memory.append_chat_turn("user", text, "general")
    memory.append_chat_turn("assistant", response, "general")


def main() -> None:
    """CLI entry point, called by cron every 2 minutes."""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    config = load_config()

    todoist = todoist_from_env()
    memory = MemoryStore(config["memory"]["db_path"])
    claude = ClaudeClient.from_env(config)
    telegram = TelegramClient.from_env(config)

    try:
        run_listener(todoist, memory, claude, telegram, config)
    except Exception as e:
        logger.error(f"Listener failed: {e}", exc_info=True)
        try:
            telegram.send_error(f"Listener failed: {e}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
