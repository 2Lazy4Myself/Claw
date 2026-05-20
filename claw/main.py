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
from claw.telegram_client import TelegramClient
from claw.todoist_client import from_env as todoist_from_env
from claw import listener, orchestrator

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 30 * 60


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
        while True:
            try:
                updates = telegram.get_updates(offset=offset, timeout=30)
                for u in updates:
                    offset = u["update_id"] + 1
                    memory.set_listener_offset(offset)
                    incoming.put(u)
            except Exception as e:
                logger.warning(f"Polling error: {e}")
                time.sleep(5)

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
