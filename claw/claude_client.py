"""
claude_client.py

Responsibility: Thin wrapper around the Anthropic API.

This module handles: auth, retry on transient errors, response parsing,
and raising descriptive errors on failure. It knows nothing about tasks,
memory, or Telegram.

All callers get back a plain string. Format parsing (e.g. extracting JSON
from a task selection response) happens in the calling module, not here.
"""

from __future__ import annotations
import json
import logging
import os
import time
import anthropic

logger = logging.getLogger(__name__)


class ClaudeClient:
    """
    Wraps the Anthropic Messages API for synchronous, single-turn completions.

    For multi-turn probe conversations, the caller maintains the message history
    and passes it to complete_with_history().
    """

    def __init__(self, api_key: str, config: dict) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._config = config

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int,
        retries: int = 2,
        model: str = None,
    ) -> str:
        """
        Single-turn completion. Returns the assistant's text response.

        Args:
            system: System prompt.
            user: User message.
            max_tokens: Maximum response tokens.
            retries: Number of retry attempts on transient errors.
            model: Override the default model. Pass config["claude"]["selection_model"]
                   for cheap/fast calls (task selection, session summary).

        Returns:
            Assistant response as a plain string.

        Raises:
            ClaudeAPIError: On unrecoverable API error.
        """
        return self._call_with_retry(
            messages=[{"role": "user", "content": user}],
            system=system,
            max_tokens=max_tokens,
            retries=retries,
            model=model,
        )

    def complete_with_history(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int,
        retries: int = 2,
        model: str = None,
    ) -> str:
        """
        Multi-turn completion. Caller provides the full message history.

        Args:
            system: System prompt.
            messages: List of {"role": "user"|"assistant", "content": "..."} dicts.
            max_tokens: Maximum response tokens.
            model: Override the default model (see complete()).

        Returns:
            Assistant response as a plain string.
        """
        return self._call_with_retry(
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            retries=retries,
            model=model,
        )

    def _call_with_retry(
        self,
        messages: list[dict],
        system: str,
        max_tokens: int,
        retries: int,
        model: str = None,
    ) -> str:
        model = model or self._config["claude"]["model"]
        last_error = None

        for attempt in range(retries + 1):
            try:
                response = self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                )
                text = response.content[0].text
                logger.debug(f"Claude responded ({len(text)} chars)")
                return text

            except anthropic.RateLimitError as e:
                wait = 2 ** attempt
                logger.warning(f"Rate limited, retrying in {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
                last_error = e

            except anthropic.APIConnectionError as e:
                wait = 2 ** attempt
                logger.warning(f"Connection error, retrying in {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
                last_error = e

            except anthropic.APIError as e:
                raise ClaudeAPIError(f"Anthropic API error: {e}") from e

        raise ClaudeAPIError(f"Claude API failed after {retries + 1} attempts: {last_error}")

    @classmethod
    def from_env(cls, config: dict) -> "ClaudeClient":
        """
        Factory that reads ANTHROPIC_API_KEY from the environment.

        Raises:
            EnvironmentError: If the key is not set.
        """
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set")
        return cls(api_key=key, config=config)


class ClaudeAPIError(Exception):
    """Raised when the Claude API returns an unrecoverable error."""
    pass
