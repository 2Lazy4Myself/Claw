"""
Unit tests for the SQLite backup path (C1).

Uses a real temp-file DB (the backup API is the unit under test), no network.

Run with: pytest tests/unit/
"""

import os

from claw.memory import MemoryStore, TaskMemory
from claw.nightly import _prune_backups, _run_backup


def _seed(db_path: str) -> MemoryStore:
    m = MemoryStore(db_path)
    m.upsert_task_memory(TaskMemory(
        task_id="t-1",
        last_probed_at=None,
        probe_count=3,
        last_outcome="committed",
        notes="[2026-06-14] made progress",
        snoozed_until=None,
        context_summary=None,
    ))
    return m


class TestBackup:
    def test_backup_is_a_valid_restorable_copy(self, tmp_path):
        m = _seed(str(tmp_path / "claw.db"))
        dest = str(tmp_path / "backups" / "snap.db")

        returned = m.backup(dest)

        assert returned == dest
        assert os.path.exists(dest)
        # The snapshot opens cleanly and carries the seeded row.
        restored = MemoryStore(dest)
        tm = restored.get_task_memory("t-1")
        assert tm is not None
        assert tm.probe_count == 3

    def test_run_backup_writes_dated_snapshot(self, tmp_path):
        m = _seed(str(tmp_path / "claw.db"))
        backup_dir = str(tmp_path / "backups")

        dest = _run_backup(m, backup_dir, retention=7)

        assert os.path.exists(dest)
        assert os.path.basename(dest).startswith("claw-")
        assert dest.endswith(".db")

    def test_prune_keeps_only_most_recent(self, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        for day in ("20260601", "20260602", "20260603", "20260604"):
            (backup_dir / f"claw-{day}.db").write_text("x")

        _prune_backups(str(backup_dir), retention=2)

        remaining = sorted(p.name for p in backup_dir.glob("claw-*.db"))
        assert remaining == ["claw-20260603.db", "claw-20260604.db"]

    def test_prune_retention_zero_is_noop(self, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "claw-20260601.db").write_text("x")

        _prune_backups(str(backup_dir), retention=0)

        assert (backup_dir / "claw-20260601.db").exists()
