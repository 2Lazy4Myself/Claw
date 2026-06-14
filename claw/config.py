"""
config.py

Responsibility: Load and validate configuration from config/config.yaml and .env.

Configuration is always loaded from files — never from hardcoded defaults in
other modules. This is the single place where config loading happens.

Environment variables (secrets) are loaded via python-dotenv.
Structured config (tunable values) is loaded from config/config.yaml.
"""

from __future__ import annotations
from pathlib import Path
import yaml
from dotenv import load_dotenv


def load_config(config_path: str = "config/config.yaml") -> dict:
    """
    Loads and returns the full config dict.

    Loads .env into the environment as a side effect (idempotent).
    Does NOT return secrets — those are read directly from os.environ
    by each client module's from_env() factory.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Parsed config dict.

    Raises:
        FileNotFoundError: If config.yaml does not exist.
        ValueError: If required keys are missing.
    """
    load_dotenv()

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {config_path}. "
            "Copy config/config.example.yaml to config/config.yaml and fill it in."
        )

    with open(path) as f:
        config = yaml.safe_load(f)

    _validate(config)
    return config


def _validate(config: dict) -> None:
    """
    Validates that required config keys are present.
    Raises ValueError with a helpful message if anything is missing.
    """
    required = [
        ("telegram", "allowed_user_id"),
        ("todoist",),
        ("memory", "db_path"),
        ("litellm", "base_url"),
        ("claude", "model"),
        ("claude", "selection_model"),
        ("schedule", "timezone"),
        ("schedule", "active_window_start"),
        ("schedule", "active_window_end"),
        ("schedule", "briefing_window_end"),
        ("schedule", "min_minutes_between_sessions"),
    ]
    for key_path in required:
        node = config
        for key in key_path:
            if not isinstance(node, dict) or key not in node:
                raise ValueError(
                    f"Missing required config key: {'.'.join(str(k) for k in key_path)}"
                )
            node = node[key]

    # Validate schedule time fields up front so malformed values fail loudly at
    # startup, not mid-tick deep inside the orchestrator (orchestrator._parse_hhmm).
    schedule = config["schedule"]
    time_fields = [
        "active_window_start",
        "active_window_end",
        "briefing_window_end",
        "nightly_synthesis_after",  # optional; validated only if present
    ]
    for field in time_fields:
        if field in schedule:
            _validate_hhmm(f"schedule.{field}", schedule[field])


def _validate_hhmm(name: str, value) -> None:
    """Raises ValueError unless value is a well-formed 'HH:MM' 24-hour time."""
    if not isinstance(value, str):
        raise ValueError(f"Config {name} must be an 'HH:MM' string, got {value!r}")
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Config {name} must be 'HH:MM', got {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Config {name} out of range (00:00–23:59), got {value!r}")
