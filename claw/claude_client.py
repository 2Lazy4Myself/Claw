"""
claude_client.py

Responsibility: Thin wrapper around an OpenAI-compatible AI API (LiteLLM proxy).

This module handles: auth, retry on transient errors, response parsing,
and raising descriptive errors on failure. It knows nothing about tasks,
memory, or Telegram.

All callers get back a plain string. Format parsing (e.g. extracting JSON
from a task selection response) happens in the calling module, not here.

Calls are routed through LiteLLM at the configured base_url. Two tiers:
  - config["claude"]["model"]            → powerful model (probe, briefing)
  - config["claude"]["selection_model"]  → cheap model (selection, summaries)
"""

from __future__ import annotations
import logging
import os
import re
import time
import openai

_THINK_RE = re.compile(r'<think>.*?</think>\s*', re.DOTALL)
_THINK_OPEN_RE = re.compile(r'<think>.*', re.DOTALL)  # unclosed tag (truncated response)

logger = logging.getLogger(__name__)


class ClaudeClient:
    """
    Wraps an OpenAI-compatible chat completions API (LiteLLM proxy).

    For multi-turn probe conversations, the caller maintains the message history
    and passes it to complete_with_history().
    """

    def __init__(self, base_url: str, api_key: str, config: dict) -> None:
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key)
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
        full_messages = [{"role": "system", "content": system}] + messages
        last_error = None

        for attempt in range(retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=full_messages,
                )
                content = response.choices[0].message.content or ""
                text = _THINK_OPEN_RE.sub('', _THINK_RE.sub('', content)).strip()
                logger.debug(f"AI responded ({len(text)} chars)")
                return text

            except openai.RateLimitError as e:
                wait = 2 ** attempt
                logger.warning(f"Rate limited, retrying in {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
                last_error = e

            except openai.APIConnectionError as e:
                wait = 2 ** attempt
                logger.warning(f"Connection error, retrying in {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
                last_error = e

            except openai.APIError as e:
                raise ClaudeAPIError(f"AI API error: {e}") from e

        raise ClaudeAPIError(f"AI API failed after {retries + 1} attempts: {last_error}")

    @classmethod
    def from_env(cls, config: dict) -> "ClaudeClient":
        """
        Factory that reads LITELLM_API_KEY and optionally LITELLM_BASE_URL from the environment.
        Falls back to config["litellm"]["base_url"] if the env var is not set.

        Raises:
            EnvironmentError: If LITELLM_API_KEY is not set.
        """
        base_url = os.environ.get("LITELLM_BASE_URL") or config["litellm"]["base_url"]
        api_key = os.environ.get("LITELLM_API_KEY")
        if not api_key:
            raise EnvironmentError("LITELLM_API_KEY is not set")
        return cls(base_url=base_url, api_key=api_key, config=config)


class ClaudeAPIError(Exception):
    """Raised when the AI API returns an unrecoverable error."""
    pass
