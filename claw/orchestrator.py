"""
orchestrator.py

Responsibility: Decide on each 30-minute tick whether to brief, probe, or stay silent.

Replaces the two fixed cron entries (08:00 briefing, 18:00 probe) with a single
frequent job that applies time-window rules and a session cooldown.

Decision logic:
    1. Outside active window (e.g. 07:00–21:00)? → exit silently
    2. Within morning window AND no briefing sent today? → run_briefing()
    3. Time since last session < min_gap? → exit silently
    4. Otherwise → run_probe()
"""

from __future__ import annotations
import logging
import queue
import sys
from datetime import datetime, timezone, date as _date, time as _time
from typing import Optional

from zoneinfo import ZoneInfo

from claw.briefing import run_briefing
from claw.claude_client import ClaudeClient
from claw.config import load_config
from claw.memory import MemoryStore
from claw.probe import run_probe
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, from_env as todoist_from_env

logger = logging.getLogger(__name__)


def run_orchestrator(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
    reply_queue: Optional[queue.Queue] = None,
) -> None:
    """
    Core orchestration logic. All dependencies injected for testability.
    reply_queue is passed through to run_probe() for daemon-mode reply routing.
    """
    tz = ZoneInfo(config["schedule"]["timezone"])
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date().isoformat()

    if not _within_active_window(config, now_local):
        logger.info("Outside active window — exiting")
        return

    if _briefing_window_open(config, now_local) and not _briefing_sent_today(memory, today_local):
        logger.info("Morning window open, no briefing today — running briefing")
        run_briefing(todoist, memory, claude, telegram, config)
        return

    if _weekly_review_due(config, now_local, memory):
        logger.info("Weekly review due — running")
        from claw.weekly import run_weekly_review
        run_weekly_review(todoist, memory, claude, telegram, config)
        return

    if _nightly_window_open(config, now_local) and not _nightly_run_today(memory, today_local):
        logger.info("Nightly window open — running synthesis")
        from claw.nightly import run_nightly
        run_nightly(memory, claude, config, telegram)
        return

    minutes = _minutes_since_last_session(memory, now_utc)
    min_gap = config["schedule"]["min_minutes_between_sessions"]
    if minutes is not None and minutes < min_gap:
        logger.info(f"Last session {minutes:.0f} min ago (gap: {min_gap}) — silent")
        return

    logger.info("Active window, gap clear — running probe")
    run_probe(todoist, memory, claude, telegram, config, reply_queue)


# ─── Pure decision functions ──────────────────────────────────────────────────

def _within_active_window(config: dict, now_local: datetime) -> bool:
    """True if current local time is within the configured active window."""
    start = _parse_hhmm(config["schedule"]["active_window_start"])
    end = _parse_hhmm(config["schedule"]["active_window_end"])
    current = now_local.time().replace(second=0, microsecond=0)
    return start <= current < end


def _briefing_window_open(config: dict, now_local: datetime) -> bool:
    """True if current local time is within the morning briefing window."""
    start = _parse_hhmm(config["schedule"]["active_window_start"])
    end = _parse_hhmm(config["schedule"]["briefing_window_end"])
    current = now_local.time().replace(second=0, microsecond=0)
    return start <= current < end


def _briefing_sent_today(memory: MemoryStore, today_iso: str) -> bool:
    """True if a briefing session has already been logged today (local date)."""
    last_date = memory.get_last_briefing_date()
    return last_date == today_iso


def _nightly_window_open(config: dict, now_local: datetime) -> bool:
    """True if current local time is in the nightly synthesis window (after configured time, before active_window_end)."""
    after = _parse_hhmm(config["schedule"].get("nightly_synthesis_after", "20:00"))
    end = _parse_hhmm(config["schedule"]["active_window_end"])
    current = now_local.time().replace(second=0, microsecond=0)
    return after <= current < end


def _nightly_run_today(memory: MemoryStore, today_iso: str) -> bool:
    """True if a nightly synthesis session has already been logged today (local date)."""
    return memory.get_last_nightly_date() == today_iso


def _weekly_review_due(config: dict, now_local: datetime, memory: MemoryStore) -> bool:
    """
    True if today is the configured review day (default Sunday), the time is within the
    nightly window, and no weekly review has run yet this ISO week.
    """
    review_day = config["schedule"].get("weekly_review_day", 6)  # Mon=0 … Sun=6
    if now_local.weekday() != review_day:
        return False
    if not _nightly_window_open(config, now_local):
        return False

    last = memory.get_last_weekly_date()
    if last is None:
        return True
    last_week = _date.fromisoformat(last).isocalendar()[:2]  # (iso_year, iso_week)
    return last_week != now_local.date().isocalendar()[:2]


def _minutes_since_last_session(memory: MemoryStore, now_utc: datetime) -> Optional[float]:
    """
    Returns minutes elapsed since the most recent session (any type).
    Returns None if no sessions have ever been logged.
    """
    last = memory.get_last_session_at()
    if last is None:
        return None
    return (now_utc - last).total_seconds() / 60


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_hhmm(value: str) -> _time:
    """Parses 'HH:MM' string to a time object."""
    h, m = value.split(":")
    return _time(int(h), int(m))


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point — called by cron every 30 minutes."""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    config = load_config()

    todoist = todoist_from_env()
    memory = MemoryStore(config["memory"]["db_path"])
    claude = ClaudeClient.from_env(config)
    telegram = TelegramClient.from_env(config)

    try:
        run_orchestrator(todoist, memory, claude, telegram, config)
    except Exception as e:
        logger.error(f"Orchestrator failed: {e}", exc_info=True)
        try:
            telegram.send_error(f"Orchestrator failed: {e}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
