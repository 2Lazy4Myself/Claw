"""
telegram_client.py

Responsibility: Send messages and receive replies via the Telegram Bot API.

This module knows nothing about tasks, prompts, or memory. It handles:
- Sending messages to the configured user
- Polling for a reply within a timeout window (used by probe sessions)
- Sending error alerts

For MVP, we use long-polling (getUpdates). This module encapsulates that
entirely — if we switch to webhooks in Phase 2, only this file changes.
"""

from __future__ import annotations
import logging
import math
import os
import time
from typing import Optional
import requests

logger = logging.getLogger(__name__)


class TelegramClient:
    """
    Sends messages and receives replies via the Telegram Bot API.

    Designed for a single-user bot. All messages go to/from allowed_user_id.
    """

    BASE_URL = "https://api.telegram.org/bot{token}"

    def __init__(self, token: str, allowed_user_id: int, error_chat_id: Optional[int] = None) -> None:
        self._token = token
        self._allowed_user_id = allowed_user_id
        self._error_chat_id = error_chat_id or allowed_user_id
        self._base = self.BASE_URL.format(token=token)

    def send_message(self, text: str) -> None:
        """
        Sends a message to the configured user.

        Raises:
            TelegramAPIError: If the API call fails.
        """
        self._post("sendMessage", {
            "chat_id": self._allowed_user_id,
            "text": text,
        })

    def get_updates(self, offset: Optional[int] = None, timeout: int = 0) -> list[dict]:
        """
        Fetches pending updates from Telegram. Non-blocking by default (timeout=0).
        Used by the listener to process inbound messages without a reply window.
        """
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = self._get("getUpdates", params)
            return resp.get("result", [])
        except TelegramAPIError as e:
            logger.warning(f"get_updates failed: {e}")
            return []

    def send_error(self, text: str) -> None:
        """
        Sends an error alert. Swallows its own exceptions to avoid error loops.
        """
        try:
            self._post("sendMessage", {
                "chat_id": self._error_chat_id,
                "text": f"⚠️ Claw error:\n{text}",
            })
        except Exception as e:
            logger.error(f"Failed to send Telegram error alert: {e}")

    def wait_for_reply(self, timeout_seconds: int = 300) -> Optional[str]:
        """
        Polls for a reply from the allowed user within the timeout window.

        Returns the user's message text, or None if no reply arrived in time.
        Only accepts messages from allowed_user_id. Ignores all others.

        This is a blocking call. Only call it after sending a message that
        expects a reply (i.e. during a probe session).

        Uses long-polling: getUpdates with a server-side timeout so we don't
        hammer the API. Tracks offset to avoid replaying old messages.
        """
        deadline = time.monotonic() + timeout_seconds
        offset: Optional[int] = None

        # Drain any pre-existing updates so we only receive new messages
        offset = self._drain_updates()

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            poll_timeout = min(30, math.floor(remaining))

            try:
                params: dict = {"timeout": poll_timeout}
                if offset is not None:
                    params["offset"] = offset

                resp = self._get("getUpdates", params)
                updates = resp.get("result", [])

                for update in updates:
                    offset = update["update_id"] + 1
                    message = update.get("message") or update.get("edited_message")
                    if not message:
                        continue
                    from_id = message.get("from", {}).get("id")
                    text = message.get("text", "")
                    if from_id == self._allowed_user_id and text:
                        return text

            except TelegramAPIError as e:
                logger.warning(f"Telegram poll error (retrying): {e}")
                time.sleep(2)

        return None

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _drain_updates(self) -> Optional[int]:
        """
        Fetches any pending updates with timeout=0 and returns the next offset.
        Prevents wait_for_reply from returning stale messages.
        """
        try:
            resp = self._get("getUpdates", {"timeout": 0})
            updates = resp.get("result", [])
            if updates:
                return updates[-1]["update_id"] + 1
        except TelegramAPIError:
            pass
        return None

    def _post(self, method: str, payload: dict) -> dict:
        url = f"{self._base}/{method}"
        response = requests.post(url, json=payload, timeout=10)
        if not response.ok:
            raise TelegramAPIError(
                f"Telegram API error {response.status_code}: {response.text}"
            )
        return response.json()

    def _get(self, method: str, params: dict) -> dict:
        url = f"{self._base}/{method}"
        # Use a timeout slightly longer than the poll timeout so the socket doesn't close first
        poll_timeout = params.get("timeout", 0)
        response = requests.get(url, params=params, timeout=poll_timeout + 10)
        if not response.ok:
            raise TelegramAPIError(
                f"Telegram API error {response.status_code}: {response.text}"
            )
        return response.json()

    @classmethod
    def from_env(cls, config: dict) -> "TelegramClient":
        """
        Factory that reads TELEGRAM_BOT_TOKEN from environment and
        allowed_user_id from config.

        Raises:
            EnvironmentError: If the token is not set.
        """
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise EnvironmentError("TELEGRAM_BOT_TOKEN is not set")
        return cls(
            token=token,
            allowed_user_id=config["telegram"]["allowed_user_id"],
            error_chat_id=config["telegram"].get("error_chat_id"),
        )


class TelegramAPIError(Exception):
    """Raised when the Telegram API returns an error or is unreachable."""
    pass
