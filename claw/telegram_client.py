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

        Args:
            text: Message text (plain text or Markdown).

        Raises:
            TelegramAPIError: If the API call fails.
        """
        raise NotImplementedError("Phase 1 implementation")

    def send_error(self, text: str) -> None:
        """
        Sends an error alert. Uses error_chat_id (may be same as main user).
        Swallows its own exceptions to avoid error loops.
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
        Only returns messages from allowed_user_id — ignores all others.

        This is a blocking call. It should only be called after sending a message
        that expects a reply (i.e. during a probe session).

        Args:
            timeout_seconds: How long to wait before giving up.

        Returns:
            The user's reply text, or None on timeout.
        """
        raise NotImplementedError("Phase 1 implementation")

    def _post(self, method: str, payload: dict) -> dict:
        """
        Makes a POST request to the Telegram Bot API.

        Raises:
            TelegramAPIError: On non-2xx response or network failure.
        """
        url = f"{self._base}/{method}"
        response = requests.post(url, json=payload, timeout=10)
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
