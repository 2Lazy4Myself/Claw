"""
Unit tests for MemoryStore schema migration (D3).

Uses real temp-file DBs (migration is the unit under test).

Run with: pytest tests/unit/
"""

import sqlite3

from claw.memory import MemoryStore, TaskMemory, _SCHEMA_VERSION


class TestSchemaMigration:
    def test_fresh_db_is_at_current_version(self, tmp_path):
        path = str(tmp_path / "fresh.db")
        MemoryStore(path)
        v = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
        assert v == _SCHEMA_VERSION

    def test_reopen_is_idempotent(self, tmp_path):
        path = str(tmp_path / "reopen.db")
        MemoryStore(path)
        MemoryStore(path)  # simulates a daemon restart — must not raise
        v = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
        assert v == _SCHEMA_VERSION

    def test_legacy_db_without_context_summary_is_backfilled(self, tmp_path):
        path = str(tmp_path / "legacy.db")
        # Build a pre-migration task_memory table (no context_summary, user_version 0).
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE task_memory ("
            "task_id TEXT PRIMARY KEY, last_probed_at TEXT, probe_count INTEGER, "
            "last_outcome TEXT, notes TEXT, snoozed_until TEXT)"
        )
        conn.commit()
        conn.close()

        # Opening via MemoryStore should run the v1 migration (add the column).
        store = MemoryStore(path)
        store.upsert_task_memory(TaskMemory(
            task_id="t-1", last_probed_at=None, probe_count=0,
            last_outcome=None, notes="", snoozed_until=None,
            context_summary="synthesised state",
        ))
        tm = store.get_task_memory("t-1")
        assert tm is not None and tm.context_summary == "synthesised state"

        v = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
        assert v == _SCHEMA_VERSION
