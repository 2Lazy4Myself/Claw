"""
nightly.py

Responsibility: Run the nightly synthesis pass.

Called by the orchestrator once per evening (after nightly_synthesis_after time):
  1. Per-task context synthesis — for tasks with >= min_probes_for_synthesis probes,
     write a Haiku-synthesised 2-3 sentence 'current state' to task_memory.context_summary.
  2. User profile synthesis — synthesise all session summaries from the last
     profile_lookback_days days into a user trait paragraph.
  3. Notes pruning — trim task_memory.notes to the last max_notes_entries entries.
"""

from __future__ import annotations
import logging
import re
import sys
import uuid
from datetime import datetime, timezone

from claw.claude_client import ClaudeClient
from claw.config import load_config
from claw.memory import MemoryStore, TaskMemory, SessionRecord
from claw.synthesis import synthesise_task_context, synthesise_user_profile

logger = logging.getLogger(__name__)

_NOTES_ENTRY_RE = re.compile(r'(?=\[\d{4}-\d{2}-\d{2}\])', re.MULTILINE)


def run_nightly(
    memory: MemoryStore,
    claude: ClaudeClient,
    config: dict,
) -> None:
    """
    Runs the full nightly synthesis pass. All dependencies injected for testability.
    """
    min_probes = config["memory"].get("min_probes_for_synthesis", 5)
    max_entries = config["memory"].get("max_notes_entries", 10)
    lookback_days = config["memory"].get("profile_lookback_days", 30)

    # ── Per-task synthesis ────────────────────────────────────────────────────
    candidates = memory.get_all_task_ids_with_probe_count(min_count=min_probes)
    synthesised_count = 0

    for task_id, _probe_count in candidates:
        sessions = memory.get_task_sessions(task_id, limit=10)
        summary = synthesise_task_context(task_id, sessions, claude, config)

        existing = memory.get_task_memory(task_id)
        if existing is None:
            continue

        memory.upsert_task_memory(TaskMemory(
            task_id=task_id,
            last_probed_at=existing.last_probed_at,
            probe_count=existing.probe_count,
            last_outcome=existing.last_outcome,
            notes=_prune_notes(existing.notes, max_entries),
            snoozed_until=existing.snoozed_until,
            context_summary=summary or existing.context_summary,
        ))
        if summary:
            synthesised_count += 1
            logger.info(f"Synthesised context for task {task_id}")

    # ── User profile synthesis ────────────────────────────────────────────────
    recent_sessions = memory.get_sessions_since_days(lookback_days, session_type="probe")
    profile = synthesise_user_profile(recent_sessions, claude, config)
    if profile:
        memory.upsert_user_profile(profile)
        logger.info("User profile updated")

    # ── Prune rolling chat memory ─────────────────────────────────────────────
    # Chat turns only matter within the short conversation window; keep a week for
    # safety, then drop so the table doesn't grow unbounded.
    pruned = memory.prune_chat_turns(older_than_days=config["memory"].get("chat_turns_retention_days", 7))
    if pruned:
        logger.info(f"Pruned {pruned} old chat turns")

    # ── Log the run ───────────────────────────────────────────────────────────
    n_tasks = len(candidates)
    run_summary = (
        f"Synthesised {synthesised_count}/{n_tasks} task contexts; "
        f"user profile {'updated' if profile else 'unchanged'}."
    )
    logger.info(f"Nightly synthesis complete: {run_summary}")

    memory.log_session(SessionRecord(
        session_id=str(uuid.uuid4()),
        session_type="nightly",
        started_at=datetime.now(timezone.utc),
        task_id=None,
        engagement_signal=None,
        summary=run_summary,
        raw_transcript=None,
    ))


def _prune_notes(notes: str, max_entries: int) -> str:
    """
    Keeps only the last max_entries dated entries from notes.
    Entries are identified by lines starting with [YYYY-MM-DD].
    """
    if not notes.strip():
        return notes
    # Split on the start of each [YYYY-MM-DD] marker
    parts = _NOTES_ENTRY_RE.split(notes)
    # Filter out any leading non-entry text, keep last N
    entries = [p.strip() for p in parts if _NOTES_ENTRY_RE.match(p.strip())]
    if len(entries) <= max_entries:
        return notes
    return "\n".join(entries[-max_entries:])


def main() -> None:
    """CLI entry point for manual invocation or testing."""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    config = load_config()

    memory = MemoryStore(config["memory"]["db_path"])
    claude = ClaudeClient.from_env(config)

    try:
        run_nightly(memory, claude, config)
    except Exception as e:
        logger.error(f"Nightly synthesis failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
