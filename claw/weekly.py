"""
weekly.py

Responsibility: The weekly review ritual — a once-a-week reflection sent to the user.

Triggered by the orchestrator on the configured review day (default Sunday) within the
nightly window. Gathers the week's check-ins and each goal's trajectory, asks Claude for
a short reflection (what moved, what stalled, goal trend, one question ahead), and sends
it. Logged as a "weekly" session so it runs at most once per ISO week.
"""

from __future__ import annotations
import logging
import sys
import uuid
from datetime import date, datetime, timezone

from claw.claude_client import ClaudeClient
from claw.config import load_config
from claw.goals import get_goals
from claw.memory import MemoryStore, SessionRecord
from claw.telegram_client import TelegramClient
from claw.todoist_client import TodoistClient, from_env as todoist_from_env
from claw import prompts
from claw import trajectory as trajectory_mod

logger = logging.getLogger(__name__)


def run_weekly_review(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    """Runs the weekly review. Failures escalate to the Telegram error channel (A2)."""
    try:
        _run_weekly_review_inner(todoist, memory, claude, telegram, config)
    except Exception as e:
        logger.error(f"Weekly review failed: {e}", exc_info=True)
        try:
            telegram.send_error(f"Weekly review failed: {e}")
        except Exception:
            pass


def _run_weekly_review_inner(
    todoist: TodoistClient,
    memory: MemoryStore,
    claude: ClaudeClient,
    telegram: TelegramClient,
    config: dict,
) -> None:
    today = date.today()

    sessions = memory.get_sessions_since_days(7)
    session_lines = [
        f"[{s.started_at.strftime('%a %-d %b')}] [{s.session_type}] {s.summary}"
        for s in sessions
        if s.summary
    ]
    session_history = "\n".join(session_lines) if session_lines else "No check-ins logged this week."

    _, goal_tasks = todoist.get_claw_data()
    goals = get_goals(goal_tasks)
    goal_context = _build_goal_context(goals, memory, today) or "No goals configured."

    reflection = claude.complete(
        system=prompts.get_prompt("WEEKLY_REVIEW_SYSTEM"),
        user=prompts.WEEKLY_REVIEW_USER_TEMPLATE.format(
            session_history=session_history,
            goal_context=goal_context,
        ),
        max_tokens=config["claude"].get("probe_max_tokens", 2000),
    )
    telegram.send_message(reflection)

    memory.log_session(SessionRecord(
        session_id=str(uuid.uuid4()),
        session_type="weekly",
        started_at=datetime.now(timezone.utc),
        task_id=None,
        engagement_signal=None,
        summary=f"Weekly review sent ({len(session_lines)} check-ins this week).",
        raw_transcript=None,
    ))
    logger.info("Weekly review sent")


def _build_goal_context(goals, memory: MemoryStore, today: date) -> str:
    """One block per goal: progress + trend line where there's enough measurement data."""
    lines = []
    for g in goals:
        line = f"- {g.name}"
        if g.current and g.target:
            line += f" ({g.current} → {g.target})"
        elif g.target:
            line += f" (target {g.target})"
        rows = memory.get_goal_measurements(g.task_id)
        points = [
            trajectory_mod.to_measurement(r["value"], r["numeric"], r["recorded_at"])
            for r in rows
            if r["numeric"] is not None
        ]
        note = trajectory_mod.trajectory_note(g.target, g.by, points, today)
        if note:
            line += f"\n  {note}"
        lines.append(line)
    return "\n".join(lines)


def main() -> None:
    """CLI entry point for manual invocation or testing."""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    config = load_config()
    todoist = todoist_from_env()
    memory = MemoryStore(config["memory"]["db_path"])
    claude = ClaudeClient.from_env(config)
    telegram = TelegramClient.from_env(config)
    run_weekly_review(todoist, memory, claude, telegram, config)


if __name__ == "__main__":
    main()
