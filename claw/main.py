"""
main.py

Daemon entrypoint. Replaces crontab-driven execution with a single persistent process.

Architecture:
  - polling_thread: continuously calls getUpdates, feeds incoming queue
  - main loop: every 30 minutes → orchestrator.run_orchestrator(); otherwise → listener.handle_update()

The probe reads replies from the same incoming queue, so there is exactly one
consumer of the Telegram offset at all times — no offset conflicts, no lock file needed.
"""

from __future__ import annotations
import logging
import queue
import sys
import threading
import time

from claw.claude_client import ClaudeClient
from claw.config import load_config
from claw.memory import MemoryStore
from claw.telegram_client import TelegramClient, TelegramAPIError
from claw.todoist_client import from_env as todoist_from_env
from claw import listener, orchestrator

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 30 * 60

# Polling-thread resilience: back off (capped) on repeated getUpdates failures and
# alert the user once after this many consecutive failures, so Claw never goes
# silently deaf (e.g. bad token, 409 conflict) without anyone noticing.
POLL_FAILURE_ALERT_THRESHOLD = 5
POLL_BACKOFF_MAX_SECONDS = 60


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    memory = MemoryStore(config["memory"]["db_path"])
    claude = ClaudeClient.from_env(config)
    telegram = TelegramClient.from_env(config)
    todoist = todoist_from_env()

    incoming: queue.Queue = queue.Queue()

    def poll() -> None:
        offset = memory.get_listener_offset()
        logger.info(f"Polling thread started (offset={offset})")
        consecutive_failures = 0
        alerted = False
        while True:
            try:
                updates = telegram.get_updates(offset=offset, timeout=30, raise_on_error=True)
            except TelegramAPIError as e:
                consecutive_failures += 1
                backoff = min(POLL_BACKOFF_MAX_SECONDS, 2 ** consecutive_failures)
                logger.warning(
                    f"Telegram poll failed (attempt {consecutive_failures}): {e} "
                    f"— backing off {backoff}s"
                )
                if consecutive_failures >= POLL_FAILURE_ALERT_THRESHOLD and not alerted:
                    telegram.send_error(
                        f"Telegram polling has failed {consecutive_failures} times in a row — "
                        f"Claw may be receiving no messages. Last error: {e}"
                    )
                    alerted = True
                time.sleep(backoff)
                continue
            except Exception as e:
                logger.warning(f"Polling error: {e}")
                time.sleep(5)
                continue

            consecutive_failures = 0
            alerted = False
            for u in updates:
                offset = u["update_id"] + 1
                # Persist the offset at receipt (at-most-once delivery). The probe
                # consumer reads replies from this same queue via wait_for_reply and
                # never passes through handle_update, so receipt is the only point that
                # covers BOTH consumers. Trade-off: an update still sitting in `incoming`
                # if the process crashes is dropped rather than redelivered — the right
                # call here, since redelivering a stale probe reply after a restart would
                # be handled out of its conversation context.
                memory.set_listener_offset(offset)
                incoming.put(u)

    threading.Thread(target=poll, daemon=True, name="telegram-poll").start()

    last_tick = 0.0
    logger.info("Claw daemon started")

    while True:
        now = time.time()

        if now - last_tick >= TICK_INTERVAL_SECONDS:
            try:
                orchestrator.run_orchestrator(
                    todoist, memory, claude, telegram, config,
                    reply_queue=incoming,
                )
            except Exception as e:
                logger.error(f"Orchestrator tick failed: {e}", exc_info=True)
                try:
                    telegram.send_error(f"Orchestrator error: {e}")
                except Exception:
                    pass
            last_tick = time.time()
            continue

        try:
            update = incoming.get(timeout=2)
            listener.handle_update(
                update, todoist, memory, claude, telegram, config,
                reply_queue=incoming,
            )
        except queue.Empty:
            pass


if __name__ == "__main__":
    main()
