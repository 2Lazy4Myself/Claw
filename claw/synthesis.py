"""
synthesis.py

Responsibility: Produce synthesised context summaries from accumulated session history.

Both functions use the cheap selection_model (Haiku/Gemini). They are called by
nightly.py and should never be called during a live probe or briefing.
"""

from __future__ import annotations
import logging
from typing import Optional, TYPE_CHECKING

from claw import prompts

if TYPE_CHECKING:
    from claw.claude_client import ClaudeClient
    from claw.memory import SessionRecord

logger = logging.getLogger(__name__)


def synthesise_task_context(
    task_id: str,
    task_sessions: list["SessionRecord"],
    claude: "ClaudeClient",
    config: dict,
) -> Optional[str]:
    """
    Reads the last N probe summaries for a task and returns a 2-3 sentence
    'current state' paragraph. Returns None if there are no summaries to work with.
    """
    lines = [
        f"[{s.started_at.strftime('%-d %b')}] {s.summary}"
        for s in task_sessions
        if s.summary
    ]
    if not lines:
        return None

    try:
        result = claude.complete(
            system=prompts.get_prompt("TASK_CONTEXT_SYNTHESIS_SYSTEM"),
            user="Probe history:\n" + "\n".join(lines),
            max_tokens=300,
            model=config["claude"]["selection_model"],
        )
        return result.strip() or None
    except Exception as e:
        logger.warning(f"Task context synthesis failed for {task_id}: {e}")
        return None


def synthesise_user_profile(
    recent_sessions: list["SessionRecord"],
    claude: "ClaudeClient",
    config: dict,
) -> Optional[str]:
    """
    Reads all session summaries from the last N days and returns a 3-5 sentence
    user trait profile. Returns None if there are no summaries to work with.
    """
    lines = [
        f"[{s.started_at.strftime('%-d %b')}] [{s.session_type}] {s.summary}"
        for s in recent_sessions
        if s.summary
    ]
    if not lines:
        return None

    lookback_days = config["memory"].get("profile_lookback_days", 30)
    try:
        result = claude.complete(
            system=prompts.get_prompt("USER_PROFILE_SYNTHESIS_SYSTEM"),
            user=f"Session history (last {lookback_days} days):\n" + "\n".join(lines),
            max_tokens=400,
            model=config["claude"]["selection_model"],
        )
        return result.strip() or None
    except Exception as e:
        logger.warning(f"User profile synthesis failed: {e}")
        return None
