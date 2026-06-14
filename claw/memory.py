"""
memory.py

Responsibility: Persist and retrieve per-task history and per-session context.

This module is the single source of truth for what Claw remembers. It wraps a
SQLite database and exposes a clean interface. Nothing outside this module touches
the DB directly.

Schema:
    task_memory       — one row per Todoist task ID, tracks last probe date and notes
    sessions          — one row per briefing/probe run, stores summary and outcome
    pending_messages  — one row per unanswered M-coded message (probes / follow-ups)

All timestamps stored as ISO 8601 strings in UTC.
"""

from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

# Bump when adding a migration step in _migrate(). Tracked via PRAGMA user_version.
_SCHEMA_VERSION = 1


@dataclass
class TaskMemory:
    """Everything Claw remembers about a specific task."""
    task_id: str
    last_probed_at: Optional[datetime]
    probe_count: int
    last_outcome: Optional[str]  # "rescheduled" | "user_committed" | "dropped" | "no_reply"
    notes: str  # Free-text log of what the user has said about this task over time
    snoozed_until: Optional[datetime]  # If rescheduled, don't probe before this date
    context_summary: Optional[str] = None  # Haiku-synthesised 3-sentence current state (nightly)


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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_memory WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_task_memory(row)

    def upsert_task_memory(self, memory: TaskMemory) -> None:
        """Creates or updates the memory record for a task."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_memory
                    (task_id, last_probed_at, probe_count, last_outcome, notes, snoozed_until,
                     context_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    last_probed_at = excluded.last_probed_at,
                    probe_count = excluded.probe_count,
                    last_outcome = excluded.last_outcome,
                    notes = excluded.notes,
                    snoozed_until = excluded.snoozed_until,
                    context_summary = excluded.context_summary
                """,
                (
                    memory.task_id,
                    _dt_to_str(memory.last_probed_at),
                    memory.probe_count,
                    memory.last_outcome,
                    memory.notes,
                    _dt_to_str(memory.snoozed_until),
                    memory.context_summary,
                ),
            )

    def log_session(self, session: SessionRecord) -> None:
        """Appends a session record. Sessions are append-only."""
        with self._connect() as conn:
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
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (n,)
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def append_chat_turn(self, role: str, content: str, source: str) -> None:
        """Append one conversation turn to the rolling chat memory.

        role: "user" | "assistant". source: "general" | "probe" — provenance
        only, used for debugging; reads don't filter on it.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chat_turns (role, content, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (role, content, source, _dt_to_str(datetime.now(timezone.utc))),
            )

    def add_goal_measurement(
        self, goal_task_id: str, value: str, numeric: Optional[float],
        recorded_at: Optional[datetime] = None,
    ) -> None:
        """Records a dated goal measurement so trajectory/trend can be computed later."""
        when = recorded_at or datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO goal_measurements (goal_task_id, value, numeric, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                (goal_task_id, value, numeric, _dt_to_str(when)),
            )

    def get_goal_measurements(self, goal_task_id: str) -> list[dict]:
        """Returns this goal's measurements oldest-first: dicts of value/numeric/recorded_at."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT value, numeric, recorded_at FROM goal_measurements "
                "WHERE goal_task_id = ? ORDER BY recorded_at ASC",
                (goal_task_id,),
            ).fetchall()
        return [
            {"value": r["value"], "numeric": r["numeric"], "recorded_at": _str_to_dt(r["recorded_at"])}
            for r in rows
        ]

    def get_recent_chat_turns(self, within_minutes: int, limit: int) -> list[dict]:
        """Return recent chat turns as [{"role", "content"}], oldest-first.

        Only turns newer than `within_minutes` are returned — past that
        inactivity window the thread is considered closed and chat falls back
        to session-summary context. At most `limit` turns (the most recent).
        """
        cutoff = _dt_to_str(datetime.now(timezone.utc) - timedelta(minutes=within_minutes))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM chat_turns "
                "WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        # Fetched newest-first for the LIMIT; reverse to chronological order.
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def prune_chat_turns(self, older_than_days: int) -> int:
        """Delete chat turns older than N days. Returns rows removed."""
        cutoff = _dt_to_str(datetime.now(timezone.utc) - timedelta(days=older_than_days))
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM chat_turns WHERE created_at < ?", (cutoff,))
            return cur.rowcount

    def get_task_memories(self, task_ids: list[str]) -> dict[str, "TaskMemory"]:
        """Returns {task_id: TaskMemory} for all known IDs. Missing IDs are absent."""
        if not task_ids:
            return {}
        with self._connect() as conn:
            placeholders = ",".join("?" * len(task_ids))
            rows = conn.execute(
                f"SELECT * FROM task_memory WHERE task_id IN ({placeholders})",
                task_ids,
            ).fetchall()
        return {row["task_id"]: self._row_to_task_memory(row) for row in rows}

    def get_last_session_at(self) -> Optional[datetime]:
        """Returns started_at of the most recent session (any type), UTC-aware."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT started_at FROM sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        dt = _str_to_dt(row["started_at"])
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def get_last_briefing_date(self) -> Optional[str]:
        """Returns the date (YYYY-MM-DD UTC) of the most recent briefing session, or None."""
        return self._get_last_session_date("briefing")

    def get_task_sessions(self, task_id: str, limit: int = 5) -> list[SessionRecord]:
        """Returns the N most recent probe sessions for a specific task, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def get_sessions_since_days(self, days: int, session_type: Optional[str] = None) -> list[SessionRecord]:
        """Returns sessions started within the last N days, newest first.

        Pass session_type to restrict to e.g. 'probe' sessions only.
        """
        cutoff = _dt_to_str(datetime.now(timezone.utc) - timedelta(days=days))
        with self._connect() as conn:
            if session_type:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE started_at >= ? AND session_type = ? "
                    "ORDER BY started_at DESC",
                    (cutoff, session_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE started_at >= ? ORDER BY started_at DESC",
                    (cutoff,),
                ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def get_all_task_ids_with_probe_count(self, min_count: int) -> list[tuple[str, int]]:
        """Returns [(task_id, probe_count)] for tasks probed at least min_count times."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, probe_count FROM task_memory WHERE probe_count >= ?",
                (min_count,),
            ).fetchall()
        return [(row["task_id"], row["probe_count"]) for row in rows]

    def get_last_nightly_date(self) -> Optional[str]:
        """Returns the date (YYYY-MM-DD UTC) of the most recent nightly synthesis, or None."""
        return self._get_last_session_date("nightly")

    def get_last_weekly_date(self) -> Optional[str]:
        """Returns the date (YYYY-MM-DD UTC) of the most recent weekly review, or None."""
        return self._get_last_session_date("weekly")

    # ─── User profile ──────────────────────────────────────────────────────────

    def upsert_user_profile(self, summary: str) -> None:
        """Stores (or replaces) the synthesised user trait profile."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_profile (id, summary, synthesised_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    summary = excluded.summary,
                    synthesised_at = excluded.synthesised_at
                """,
                (summary, _dt_to_str(datetime.now(timezone.utc))),
            )

    def get_user_profile(self) -> Optional[str]:
        """Returns the current user profile summary, or None if not yet synthesised."""
        with self._connect() as conn:
            row = conn.execute("SELECT summary FROM user_profile WHERE id = 1").fetchone()
        return row["summary"] if row else None

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

        with self._connect() as conn:
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

    # ─── Pending messages (M-code registry) ───────────────────────────────────

    def assign_message_code(self, text: str, msg_type: str, cap: int = 3) -> Optional[str]:
        """
        Assigns the lowest free M-code (M1–M9) for an outbound probe/follow-up.
        Returns the code string, or None if all slots up to cap are already pending.
        """
        with self._connect() as conn:
            pending_codes = {
                row["code"] for row in conn.execute(
                    "SELECT code FROM pending_messages WHERE status = 'pending'"
                ).fetchall()
            }
            if len(pending_codes) >= cap:
                return None
            for i in range(1, 10):
                code = f"M{i}"
                if code not in pending_codes:
                    conn.execute(
                        "INSERT OR REPLACE INTO pending_messages (code, text, type, sent_at) "
                        "VALUES (?, ?, ?, ?)",
                        (code, text, msg_type, _dt_to_str(datetime.now(timezone.utc))),
                    )
                    return code
        return None

    def close_message_code(self, code: str) -> Optional[dict]:
        """Marks a pending message as answered. Returns the row or None if not found."""
        code = code.upper()
        now = _dt_to_str(datetime.now(timezone.utc))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_messages WHERE code = ? AND status = 'pending'",
                (code,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE pending_messages SET status = 'answered', answered_at = ? WHERE code = ?",
                (now, code),
            )
            return dict(row)

    def pending_count(self) -> int:
        """Returns the number of pending (unanswered) M-coded messages."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pending_messages WHERE status = 'pending'"
            ).fetchone()
        return row["n"] if row else 0

    def get_pending_messages(self) -> list[dict]:
        """Returns all pending message rows, ordered by code."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_messages WHERE status = 'pending' ORDER BY code"
            ).fetchall()
        return [dict(row) for row in rows]

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _get_last_session_date(self, session_type: str) -> Optional[str]:
        """Returns the local date (YYYY-MM-DD UTC) of the most recent session of a given type."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT started_at FROM sessions WHERE session_type = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (session_type,),
            ).fetchone()
        if row is None:
            return None
        dt = _str_to_dt(row["started_at"])
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date().isoformat()

    def get_listener_offset(self) -> Optional[int]:
        """Returns the last processed Telegram update_id + 1, or None if never set."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM listener_state WHERE key = 'telegram_offset'"
            ).fetchone()
        if row is None:
            return None
        return int(row["value"])

    def set_listener_offset(self, offset: int) -> None:
        """Persists the next Telegram update offset."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO listener_state (key, value) VALUES ('telegram_offset', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(offset),),
            )

    # Idempotency for inbound updates. Telegram delivery is at-least-once: if the
    # ack round-trip to getUpdates is lost, an already-enqueued update is redelivered.
    # Recording handled update_ids makes reprocessing a no-op (see ADR-014).
    _HANDLED_RETENTION = 10_000  # rows kept below the latest update_id; table is bounded

    def already_handled(self, update_id: int) -> bool:
        """True if this Telegram update_id has already been processed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM handled_updates WHERE update_id = ?", (update_id,)
            ).fetchone()
        return row is not None

    def mark_handled(self, update_id: int) -> None:
        """Records a Telegram update_id as processed, pruning old rows to stay bounded."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO handled_updates (update_id, handled_at) VALUES (?, ?)
                ON CONFLICT(update_id) DO NOTHING
                """,
                (update_id, datetime.now(timezone.utc).isoformat()),
            )
            # update_ids increase monotonically, so a simple low-water cutoff bounds the table
            conn.execute(
                "DELETE FROM handled_updates WHERE update_id < ?",
                (update_id - self._HANDLED_RETENTION,),
            )

    def _init_schema(self) -> None:
        with self._connect() as conn:
            # WAL improves crash resilience and lets reads proceed during writes.
            # The mode is persisted in the DB file, so this only needs setting once;
            # in-memory DBs ignore it harmlessly.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_memory (
                    task_id TEXT PRIMARY KEY,
                    last_probed_at TEXT,
                    probe_count INTEGER DEFAULT 0,
                    last_outcome TEXT,
                    notes TEXT DEFAULT '',
                    snoozed_until TEXT,
                    context_summary TEXT
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_messages (
                    code        TEXT PRIMARY KEY,
                    text        TEXT NOT NULL,
                    type        TEXT NOT NULL,
                    sent_at     TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    answered_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_profile (
                    id          INTEGER PRIMARY KEY CHECK (id = 1),
                    summary     TEXT NOT NULL,
                    synthesised_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS handled_updates (
                    update_id  INTEGER PRIMARY KEY,
                    handled_at TEXT NOT NULL
                )
            """)
            # Rolling short-term memory for open chat: each user/assistant turn
            # in general conversation, plus probe turns flushed on close. Read
            # back as a recency-windowed thread so chat is no longer stateless.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_turns (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    role       TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    source     TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS goal_measurements (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_task_id  TEXT NOT NULL,
                    value         TEXT NOT NULL,
                    numeric       REAL,
                    recorded_at   TEXT NOT NULL
                )
            """)

            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """
        Applies ordered schema migrations, tracked via PRAGMA user_version so each
        step runs once and the DB's schema level is visible/inspectable. Add a new
        `if version < N:` block and bump _SCHEMA_VERSION for each future change.
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= _SCHEMA_VERSION:
            return

        if version < 1:
            # v1: context_summary on task_memory (already in the CREATE for new DBs;
            # this back-fills DBs created before the column existed).
            try:
                conn.execute("ALTER TABLE task_memory ADD COLUMN context_summary TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists

        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        logger.info(f"Schema migrated: user_version {version} → {_SCHEMA_VERSION}")

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """
        Yields a connection that commits on success, rolls back on exception, and
        is always closed. Replaces the bare `with self._get_connection()` pattern,
        which committed but leaked the connection (relying on GC to close it).
        """
        conn = self._get_connection()
        try:
            with conn:  # commit on success / rollback on exception
                yield conn
        finally:
            conn.close()

    def backup(self, dest_path: str) -> str:
        """
        Writes a consistent snapshot of the DB to dest_path using SQLite's online
        backup API — safe to call while the daemon is live — then verifies the copy
        with PRAGMA integrity_check.

        Returns the destination path. Raises if the integrity check does not pass.
        """
        dest_dir = os.path.dirname(dest_path)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)

        src = self._get_connection()
        try:
            dest = sqlite3.connect(dest_path)
            try:
                with dest:
                    src.backup(dest)
                row = dest.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    raise RuntimeError(f"Backup integrity check failed: {row}")
            finally:
                dest.close()
        finally:
            src.close()
        return dest_path

    def _row_to_task_memory(self, row: sqlite3.Row) -> TaskMemory:
        return TaskMemory(
            task_id=row["task_id"],
            last_probed_at=_str_to_dt(row["last_probed_at"]),
            probe_count=row["probe_count"],
            last_outcome=row["last_outcome"],
            notes=row["notes"] or "",
            snoozed_until=_str_to_dt(row["snoozed_until"]),
            context_summary=row["context_summary"],
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
