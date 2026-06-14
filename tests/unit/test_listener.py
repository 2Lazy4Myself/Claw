"""
Unit tests for the inbound message listener (B1).

Covers M-code parsing, the intent regex fallback, and handle_update routing
guards (allowed-user filter + at-least-once idempotency). No network/DB.

Run with: pytest tests/unit/
"""

from unittest.mock import MagicMock

from claw import listener
from claw.listener import _parse_code_replies, _handle_code_replies, _INTENT_RE


class TestParseCodeReplies:
    def test_single_code(self):
        assert _parse_code_replies("M2 - yeah") == [("M2", "yeah")]

    def test_multiple_codes(self):
        assert _parse_code_replies("M2 - yeah, M1 - No") == [("M2", "yeah"), ("M1", "No")]

    def test_lowercase_is_normalised(self):
        assert _parse_code_replies("m3 done") == [("M3", "done")]

    def test_code_with_no_reply_text(self):
        assert _parse_code_replies("M1") == [("M1", "")]

    def test_no_code_returns_empty(self):
        assert _parse_code_replies("just a normal message") == []

    def test_various_separators_stripped(self):
        # leading -, :, – and trailing ,; are stripped from the reply
        assert _parse_code_replies("M4: rescheduled;") == [("M4", "rescheduled")]

    def test_code_must_be_word_boundary(self):
        # "ALARM2" should not be read as M2 (\b prevents mid-word matches)
        assert _parse_code_replies("ALARM2 went off") == []


class TestIntentRegexFallback:
    def test_extracts_intent_from_truncated_json(self):
        m = _INTENT_RE.search('{"intent": "briefing", "extra')
        assert m and m.group(1) == "briefing"

    def test_no_match_on_unknown_intent(self):
        assert _INTENT_RE.search('{"intent": "dance"}') is None


class TestHandleCodeReplies:
    def test_closed_and_unknown_codes_acked_separately(self):
        memory = MagicMock()
        # M2 closes (returns a row), M5 is unknown (returns None/falsy)
        memory.close_message_code.side_effect = lambda code: {"M2": {"code": "M2"}}.get(code)
        telegram = MagicMock()

        _handle_code_replies([("M2", "yeah"), ("M5", "what")], memory, telegram)

        sent = " ".join(c.args[0] for c in telegram.send_message.call_args_list)
        assert "M2 closed" in sent
        assert "No pending message for M5" in sent


class TestHandleUpdateGuards:
    def _update(self, uid=10, user_id=123, text="hello"):
        return {"update_id": uid, "message": {"from": {"id": user_id}, "text": text}}

    def _config(self):
        return {"telegram": {"allowed_user_id": 123}}

    def test_ignores_other_users(self, monkeypatch):
        dispatched = MagicMock()
        monkeypatch.setattr(listener, "_handle_message", dispatched)
        memory = MagicMock()

        listener.handle_update(
            self._update(user_id=999), MagicMock(), memory,
            MagicMock(), MagicMock(), self._config(),
        )

        dispatched.assert_not_called()
        memory.mark_handled.assert_not_called()

    def test_skips_already_handled(self, monkeypatch):
        dispatched = MagicMock()
        monkeypatch.setattr(listener, "_handle_message", dispatched)
        memory = MagicMock()
        memory.already_handled.return_value = True

        listener.handle_update(
            self._update(), MagicMock(), memory,
            MagicMock(), MagicMock(), self._config(),
        )

        dispatched.assert_not_called()
        memory.mark_handled.assert_not_called()

    def test_dispatches_and_marks_handled(self, monkeypatch):
        dispatched = MagicMock()
        monkeypatch.setattr(listener, "_handle_message", dispatched)
        memory = MagicMock()
        memory.already_handled.return_value = False

        listener.handle_update(
            self._update(uid=42), MagicMock(), memory,
            MagicMock(), MagicMock(), self._config(),
        )

        dispatched.assert_called_once()
        memory.mark_handled.assert_called_once_with(42)
