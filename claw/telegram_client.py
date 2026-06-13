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
import queue
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

    _MAX_LEN = 4096

    def send_message(self, text: str) -> None:
        """
        Sends a message to the configured user, splitting on paragraph breaks
        if the text exceeds Telegram's 4096-character limit.

        Raises:
            TelegramAPIError: If the API call fails.
        """
        for chunk in self._split(text):
            self._post("sendMessage", {
                "chat_id": self._allowed_user_id,
                "text": chunk,
            })

    def _split(self, text: str) -> list[str]:
        """Split text into ≤4096-char chunks, preferring paragraph breaks."""
        if len(text) <= self._MAX_LEN:
            return [text]

        chunks: list[str] = []
        while len(text) > self._MAX_LEN:
            # Try to break at a paragraph boundary
            cut = text.rfind("\n\n", 0, self._MAX_LEN)
            if cut == -1:
                # Fall back to any newline
                cut = text.rfind("\n", 0, self._MAX_LEN)
            if cut == -1:
                # Hard cut — no newline found in window
                cut = self._MAX_LEN
            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip()
        if text:
            chunks.append(text)
        return chunks

    def get_updates(
        self,
        offset: Optional[int] = None,
        timeout: int = 0,
        raise_on_error: bool = False,
    ) -> list[dict]:
        """
        Fetches pending updates from Telegram. Non-blocking by default (timeout=0).
        Used by the listener to process inbound messages without a reply window.

        By default API failures are swallowed and an empty list is returned (the
        caller treats a failed call as "no updates"). The daemon's polling thread
        passes raise_on_error=True so it can distinguish a genuine idle poll from a
        persistent failure (bad token, 409 conflict) and back off / alert instead of
        silently looping — see claw/main.py poll().
        """
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = self._get("getUpdates", params)
            return resp.get("result", [])
        except TelegramAPIError as e:
            if raise_on_error:
                raise
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

    def wait_for_reply(
        self,
        timeout_seconds: int = 300,
        reply_queue: Optional[queue.Queue] = None,
    ) -> Optional[str]:
        """
        Waits for a reply from the allowed user within the timeout window.

        When reply_queue is provided (daemon mode), reads from the shared queue
        that the background polling thread fills. When None (script/test mode),
        falls back to direct long-polling against the Telegram API.

        Returns the user's message text, or None if no reply arrived in time.
        """
        if reply_queue is not None:
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    update = reply_queue.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                msg = update.get("message")
                if not msg:  # ignore edited_message — edit events are not new replies
                    continue
                if msg.get("from", {}).get("id") != self._allowed_user_id:
                    continue
                text = msg.get("text", "")
                if text:
                    return text
            return None

        # Direct long-poll path (script / test mode)
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
                    message = update.get("message")
                    if not message:  # ignore edited_message
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
