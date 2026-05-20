"""
listener.py

Responsibility: Process inbound Telegram messages outside of probe sessions.

Designed to run as a frequent cron job (every 2 minutes). Each run:
  1. Exits immediately if a probe session is active (lock file check)
  2. Fetches pending Telegram updates since last processed offset
  3. Handles each message from the allowed user
  4. Persists the new offset so the next run doesn't reprocess messages

Message handling:
  - "briefing" intent  → run a full morning briefing
  - "general" intent   → Claude responds with session context

Snooze requests during a probe are handled by probe.py. The listener doesn't
manage snooze independently because it lacks the task-conversation context.
"""

from __future__ import annotations
import json
import logging
import os
import re
import sys

from claw.config import load_config
from claw.memory import MemoryStore, build_context_block
from claw.claude_client import ClaudeClient
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, from_env as todoist_from_env
from claw import prompts

PROBE_LOCK_FILE = "/tmp/claw_probe.lock"

# Matches any M-code (M1–M9) in a message — used to detect code replies
_CODE_RE = re.compile(r'\b(M\d)\b', re.IGNORECASE)

logger = logging.getLogger(__name__)


def run_listener(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    if os.path.exists(PROBE_LOCK_FILE):
        logger.info("Probe is active — listener exiting")
        return

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
        text = (msg.get("text") or "").strip()
        if not text:
            continue

        logger.info(f"Listener handling message: {text[:60]!r}")
        _handle_message(text, todoist, memory, claude, telegram, config)
        break  # one message per cron run

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
) -> None:
    # Fast-path: M-code replies bypass Claude intent classification
    code_replies = _parse_code_replies(text)
    if code_replies:
        _handle_code_replies(code_replies, memory, telegram)
        return

    raw = claude.complete(
        system=prompts.get_prompt("LISTENER_INTENT_SYSTEM"),
        user=text,
        max_tokens=30,
        model=config["claude"]["selection_model"],
    )
    try:
        intent = json.loads(prompts.strip_json_fences(raw)).get("intent", "general")
    except (json.JSONDecodeError, AttributeError):
        intent = "general"

    if intent == "briefing":
        _handle_briefing(todoist, memory, claude, telegram, config)
    elif intent == "probe":
        _handle_probe(todoist, memory, claude, telegram, config)
    else:
        _handle_general(text, memory, claude, telegram, config)


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
        telegram.send_message(f"Got it — {', '.join(closed)} closed.")
    if unknown:
        telegram.send_message(f"No pending message for {', '.join(unknown)}.")


def _handle_probe(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    from claw.probe import run_probe
    try:
        run_probe(todoist, memory, claude, telegram, config)
    except Exception as e:
        logger.error(f"On-demand probe failed: {e}", exc_info=True)
        telegram.send_error(f"Probe failed: {e}")


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


def _handle_general(
    text: str,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    recent_sessions = memory.get_recent_sessions(n=3)
    context = build_context_block(None, recent_sessions)
    try:
        response = claude.complete(
            system=prompts.get_prompt("LISTENER_GENERAL_SYSTEM"),
            user=f"Context:\n{context}\n\nUser message: {text}",
            max_tokens=200,
        )
        telegram.send_message(response)
    except Exception as e:
        logger.error(f"Listener general response failed: {e}", exc_info=True)
        telegram.send_error(f"Something went wrong: {e}")


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
