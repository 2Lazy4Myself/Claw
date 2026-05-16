"""
memory.py

Responsibility: Persist and retrieve per-task history and per-session context.

This module is the single source of truth for what Claw remembers. It wraps a
SQLite database and exposes a clean interface. Nothing outside this module touches
the DB directly.

Schema overview:
    tasks           — one row per Todoist task ID, tracks last probe date and notes
    probe_sessions  — one row per probe conversation, stores summary and outcome
    sessions        — one row per briefing/probe run, stores engagement signal

All timestamps stored as ISO 8601 strings in UTC.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import sqlite3
import json


@dataclass
class TaskMemory:
    """Everything Claw remembers about a specific task."""
    task_id: str
    last_probed_at: Optional[datetime]
    probe_count: int
    last_outcome: Optional[str]  # e.g. "rescheduled", "user_committed", "dropped", "no_reply"
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
    Use the context manager for write operations to ensure transactions commit.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_schema()

    def get_task_memory(self, task_id: str) -> Optional[TaskMemory]:
        """
        Returns memory for a task, or None if the task has never been discussed.
        """
        raise NotImplementedError("Phase 1 implementation")

    def upsert_task_memory(self, memory: TaskMemory) -> None:
        """
        Creates or updates the memory record for a task.
        """
        raise NotImplementedError("Phase 1 implementation")

    def log_session(self, session: SessionRecord) -> None:
        """
        Appends a session record. Sessions are append-only — never updated.
        """
        raise NotImplementedError("Phase 1 implementation")

    def get_recent_sessions(self, n: int = 5) -> list[SessionRecord]:
        """
        Returns the n most recent sessions, newest first.
        Used to give Claude context about recent engagement.
        """
        raise NotImplementedError("Phase 1 implementation")

    def get_tasks_not_recently_probed(
        self, task_ids: list[str], min_hours: int = 48
    ) -> list[str]:
        """
        Filters a list of task IDs to those not probed within min_hours.
        Returns task IDs only — caller fetches full task data from Todoist.
        """
        raise NotImplementedError("Phase 1 implementation")

    def _init_schema(self) -> None:
        """
        Creates tables if they don't exist. Safe to call on every startup.
        Schema changes require a migration — do not alter tables here after Phase 1.
        """
        raise NotImplementedError("Phase 1 implementation")

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


def build_context_block(
    task_memory: Optional[TaskMemory],
    recent_sessions: list[SessionRecord],
) -> str:
    """
    Assembles a plain-text memory context block for injection into Claude prompts.

    Kept as a pure function (not a method) so it can be tested without a DB.

    Returns a string like:
        Task history: Last discussed 3 days ago. User said it was blocked by X.
        Recent engagement: 3 sessions this week, generally responsive.
    """
    raise NotImplementedError("Phase 1 implementation")
