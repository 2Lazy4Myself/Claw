"""
Unit tests for nightly synthesis helpers (B3).

Covers notes pruning edge cases. Backup behaviour is covered in test_backup.py.

Run with: pytest tests/unit/
"""

from unittest.mock import MagicMock

from claw import nightly
from claw.nightly import _prune_notes


def _entries(n):
    return "\n".join(f"[2026-06-{i:02d}] note {i}" for i in range(1, n + 1))


class TestPruneNotes:
    def test_empty_notes_unchanged(self):
        assert _prune_notes("", 10) == ""
        assert _prune_notes("   ", 10) == "   "

    def test_under_limit_unchanged(self):
        notes = _entries(3)
        assert _prune_notes(notes, 10) == notes

    def test_exactly_at_limit_unchanged(self):
        notes = _entries(10)
        assert _prune_notes(notes, 10) == notes

    def test_over_limit_keeps_most_recent(self):
        notes = _entries(15)
        pruned = _prune_notes(notes, 5)
        # keeps the last 5 dated entries (11..15), drops the first 10
        assert "[2026-06-11] note 11" in pruned
        assert "[2026-06-15] note 15" in pruned
        assert "[2026-06-10] note 10" not in pruned
        assert "note 1\n" not in pruned and not pruned.startswith("[2026-06-01]")
        assert pruned.count("[2026-06-") == 5

    def test_leading_non_entry_text_dropped_when_over_limit(self):
        notes = "stray preamble\n" + _entries(12)
        pruned = _prune_notes(notes, 5)
        assert "stray preamble" not in pruned
        assert pruned.count("[2026-06-") == 5


class TestNightlyBackupEscalation:
    """A2: a failed nightly DB backup must reach the Telegram error channel."""

    def _empty_memory(self):
        memory = MagicMock()
        memory.get_all_task_ids_with_probe_count.return_value = []
        memory.get_sessions_since_days.return_value = []
        memory.prune_chat_turns.return_value = 0
        return memory

    def _config(self):
        return {"memory": {"backup_dir": "/some/dir"}}

    def test_backup_failure_sends_error(self, monkeypatch):
        memory = self._empty_memory()
        telegram = MagicMock()
        monkeypatch.setattr(nightly, "_run_backup", MagicMock(side_effect=OSError("disk full")))

        nightly.run_nightly(memory, MagicMock(), self._config(), telegram)

        telegram.send_error.assert_called_once()
        assert "backup failed" in telegram.send_error.call_args.args[0].lower()

    def test_backup_failure_without_telegram_does_not_raise(self, monkeypatch):
        memory = self._empty_memory()
        monkeypatch.setattr(nightly, "_run_backup", MagicMock(side_effect=OSError("disk full")))

        # telegram omitted (manual/CLI path) — must degrade quietly, not crash.
        nightly.run_nightly(memory, MagicMock(), self._config())
