"""
memory.py

Responsibility: Persist and retrieve per-task history and per-session context.

This module is the single source of truth for what Claw remembers. It wraps a
SQLite database and exposes a clean interface. Nothing outside this module touches
the DB directly.

Schema:
    task_memory     — one row per Todoist task ID, tracks last probe date and notes
    sessions        — one row per briefing/probe run, stores summary and outcome

All timestamps stored as ISO 8601 strings in UTC.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import sqlite3


@dataclass
class TaskMemory:
    """Everything Claw remembers about a specific task."""
    task_id: str
    last_probed_at: Optional[datetime]
    probe_count: int
    last_outcome: Optional[str]  # "rescheduled" | "user_committed" | "dropped" | "no_reply"
    notes: str  # Free-text log of what the user has said about this task over time
    snoozed_until: Optional[datetime]  # If rescheduled, don't probe before this date


@dataclass
class SessionRecord:
    """Record of a single briefing or probe session."""
    session_id: str
    session_type: str  # "briefing" | "probe"
    started_at: datetime
    task_id: Optional[str]  # For probe sessions
    engagement_signal: Optional[int]  # 1-5 scale, set at end of session
    summary: Optional[str]  # Claude's brief summary of what happened
    raw_transcript: Optional[str]  # Full JSON of the conversation


class MemoryStore:
    """
    Read/write interface to the Claw SQLite memory database.

    All methods are synchronous. The DB file is created on first use.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_schema()

    def get_task_memory(self, task_id: str) -> Optional[TaskMemory]:
        """Returns memory for a task, or None if never discussed."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_memory WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_task_memory(row)

    def upsert_task_memory(self, memory: TaskMemory) -> None:
        """Creates or updates the memory record for a task."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO task_memory
                    (task_id, last_probed_at, probe_count, last_outcome, notes, snoozed_until)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    last_probed_at = excluded.last_probed_at,
                    probe_count = excluded.probe_count,
                    last_outcome = excluded.last_outcome,
                    notes = excluded.notes,
                    snoozed_until = excluded.snoozed_until
                """,
                (
                    memory.task_id,
                    _dt_to_str(memory.last_probed_at),
                    memory.probe_count,
                    memory.last_outcome,
                    memory.notes,
                    _dt_to_str(memory.snoozed_until),
                ),
            )

    def log_session(self, session: SessionRecord) -> None:
        """Appends a session record. Sessions are append-only."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (session_id, session_type, started_at, task_id,
                     engagement_signal, summary, raw_transcript)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.session_type,
                    _dt_to_str(session.started_at),
                    session.task_id,
                    session.engagement_signal,
                    session.summary,
                    session.raw_transcript,
                ),
            )

    def get_recent_sessions(self, n: int = 5) -> list[SessionRecord]:
        """Returns the n most recent sessions, newest first."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (n,)
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def get_task_memories(self, task_ids: list[str]) -> dict[str, "TaskMemory"]:
        """Returns {task_id: TaskMemory} for all known IDs. Missing IDs are absent."""
        if not task_ids:
            return {}
        with self._get_connection() as conn:
            placeholders = ",".join("?" * len(task_ids))
            rows = conn.execute(
                f"SELECT * FROM task_memory WHERE task_id IN ({placeholders})",
                task_ids,
            ).fetchall()
        return {row["task_id"]: self._row_to_task_memory(row) for row in rows}

    def get_tasks_not_recently_probed(
        self, task_ids: list[str], min_hours: int = 48
    ) -> list[str]:
        """
        Filters task_ids to those not probed within min_hours.
        Returns task IDs only.
        """
        if not task_ids:
            return []
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=min_hours)
        cutoff_str = _dt_to_str(cutoff)

        with self._get_connection() as conn:
            placeholders = ",".join("?" * len(task_ids))
            rows = conn.execute(
                f"""
                SELECT task_id FROM task_memory
                WHERE task_id IN ({placeholders})
                AND last_probed_at >= ?
                """,
                (*task_ids, cutoff_str),
            ).fetchall()

        recently_probed = {row["task_id"] for row in rows}
        return [tid for tid in task_ids if tid not in recently_probed]

    # ─── Internal ─────────────────────────────────────────────────────────────

    def get_listener_offset(self) -> Optional[int]:
        """Returns the last processed Telegram update_id + 1, or None if never set."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT value FROM listener_state WHERE key = 'telegram_offset'"
            ).fetchone()
        if row is None:
            return None
        return int(row["value"])

    def set_listener_offset(self, offset: int) -> None:
        """Persists the next Telegram update offset."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO listener_state (key, value) VALUES ('telegram_offset', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(offset),),
            )

    def _init_schema(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_memory (
                    task_id TEXT PRIMARY KEY,
                    last_probed_at TEXT,
                    probe_count INTEGER DEFAULT 0,
                    last_outcome TEXT,
                    notes TEXT DEFAULT '',
                    snoozed_until TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    session_type TEXT,
                    started_at TEXT,
                    task_id TEXT,
                    engagement_signal INTEGER,
                    summary TEXT,
                    raw_transcript TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS listener_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _row_to_task_memory(self, row: sqlite3.Row) -> TaskMemory:
        return TaskMemory(
            task_id=row["task_id"],
            last_probed_at=_str_to_dt(row["last_probed_at"]),
            probe_count=row["probe_count"],
            last_outcome=row["last_outcome"],
            notes=row["notes"] or "",
            snoozed_until=_str_to_dt(row["snoozed_until"]),
        )

    def _row_to_session(self, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            session_type=row["session_type"],
            started_at=_str_to_dt(row["started_at"]),
            task_id=row["task_id"],
            engagement_signal=row["engagement_signal"],
            summary=row["summary"],
            raw_transcript=row["raw_transcript"],
        )


# ─── Pure helpers ─────────────────────────────────────────────────────────────

def build_context_block(
    task_memory: Optional[TaskMemory],
    recent_sessions: list[SessionRecord],
) -> str:
    """
    Assembles a plain-text memory context block for injection into Claude prompts.
    Pure function — no DB access.
    """
    parts: list[str] = []

    if task_memory is None:
        parts.append("Task history: No previous history for this task.")
    else:
        age = _days_ago(task_memory.last_probed_at)
        age_str = f"{age} day{'s' if age != 1 else ''} ago" if age is not None else "unknown"
        outcome_str = task_memory.last_outcome or "unknown"
        notes_str = task_memory.notes.strip() if task_memory.notes else ""
        summary = (
            f"Task history: Last discussed {age_str}. "
            f"Probed {task_memory.probe_count} time{'s' if task_memory.probe_count != 1 else ''}. "
            f"Last outcome: {outcome_str}."
        )
        if notes_str:
            # Include a trimmed snippet of the notes
            snippet = notes_str[:200] + ("…" if len(notes_str) > 200 else "")
            summary += f" Notes: {snippet}"
        parts.append(summary)

    if recent_sessions:
        count = len(recent_sessions)
        parts.append(
            f"Recent engagement: {count} recent session{'s' if count != 1 else ''}."
        )
    else:
        parts.append("Recent engagement: No recent sessions.")

    return "\n".join(parts)


# ─── Timestamp helpers ────────────────────────────────────────────────────────

def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _days_ago(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).days
