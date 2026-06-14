"""
Unit tests for inline-keyboard buttons (F4).

Covers button layout, send_message reply_markup, callback handling in
wait_for_reply, the action→reply mapping, and the listener callback route.

Run with: pytest tests/unit/
"""

import queue
from unittest.mock import MagicMock

from claw import listener, prompts
from claw.telegram_client import TelegramClient, _button_rows


def _client():
    c = TelegramClient.__new__(TelegramClient)  # bypass __init__
    c._allowed_user_id = 123
    c._token = "t"
    c._base = "https://example/bot"
    c._MAX_LEN = 4096
    c._post = MagicMock(return_value={})
    return c


class TestButtonLayout:
    def test_two_per_row(self):
        rows = _button_rows([("A", "a"), ("B", "b"), ("C", "c")])
        assert len(rows) == 2
        assert rows[0] == [
            {"text": "A", "callback_data": "a"},
            {"text": "B", "callback_data": "b"},
        ]
        assert rows[1] == [{"text": "C", "callback_data": "c"}]


class TestSendMessageButtons:
    def test_reply_markup_attached(self):
        c = _client()
        c.send_message("hi", buttons=[("✅ Done", "act:done")])
        payload = c._post.call_args.args[1]
        assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "act:done"

    def test_no_markup_without_buttons(self):
        c = _client()
        c.send_message("hi")
        assert "reply_markup" not in c._post.call_args.args[1]


class TestResolveActionReply:
    def test_maps_known_action(self):
        assert prompts.resolve_action_reply("act:done") == prompts.PROBE_ACTION_REPLIES["act:done"]

    def test_passes_through_plain_text(self):
        assert prompts.resolve_action_reply("just typing") == "just typing"

    def test_none_stays_none(self):
        assert prompts.resolve_action_reply(None) is None


class TestWaitForReplyCallback:
    def test_returns_callback_data_and_answers(self):
        c = _client()
        c.answer_callback_query = MagicMock()
        q: queue.Queue = queue.Queue()
        q.put({"callback_query": {"id": "cq1", "from": {"id": 123}, "data": "act:done"}})

        result = c.wait_for_reply(timeout_seconds=2, reply_queue=q)

        assert result == "act:done"
        c.answer_callback_query.assert_called_once_with("cq1")

    def test_ignores_callback_from_other_user(self):
        c = _client()
        c.answer_callback_query = MagicMock()
        q: queue.Queue = queue.Queue()
        q.put({"callback_query": {"id": "cq1", "from": {"id": 999}, "data": "act:done"}})

        # No valid input arrives → returns None after the short timeout.
        assert c.wait_for_reply(timeout_seconds=1, reply_queue=q) is None
        c.answer_callback_query.assert_not_called()


class TestListenerCallbackRoute:
    def test_tap_acknowledged_and_routed(self, monkeypatch):
        dispatched = MagicMock()
        monkeypatch.setattr(listener, "_handle_message", dispatched)
        memory = MagicMock()
        memory.already_handled.return_value = False
        telegram = MagicMock()
        update = {
            "update_id": 7,
            "callback_query": {"id": "cq9", "from": {"id": 123}, "data": "act:tomorrow"},
        }

        listener.handle_update(
            update, MagicMock(), memory, MagicMock(), telegram,
            {"telegram": {"allowed_user_id": 123}},
        )

        telegram.answer_callback_query.assert_called_once_with("cq9")
        # mapped text, not raw callback_data, is what gets routed
        assert dispatched.call_args.args[0] == prompts.PROBE_ACTION_REPLIES["act:tomorrow"]
        memory.mark_handled.assert_called_once_with(7)

    def test_tap_from_other_user_ignored(self):
        telegram = MagicMock()
        listener.handle_update(
            {"update_id": 1, "callback_query": {"id": "x", "from": {"id": 999}, "data": "act:done"}},
            MagicMock(), MagicMock(), MagicMock(), telegram,
            {"telegram": {"allowed_user_id": 123}},
        )
        telegram.answer_callback_query.assert_not_called()
