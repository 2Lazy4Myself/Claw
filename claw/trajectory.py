"""
trajectory.py

Responsibility: Turn a goal's measurement history into a trend — rate of change,
projected arrival vs the deadline — for prompt injection and the weekly review.

Measurements are recorded by the goal-update detector (see detectors.py) whenever
the user states a concrete value during a probe, and stored in MemoryStore. This
module is pure: it does arithmetic on already-fetched measurements and never does I/O.
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

_NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class Measurement:
    value: str            # raw value as stated, e.g. "102kg"
    numeric: float        # parsed numeric component
    recorded_at: datetime


def parse_measurement(value: Optional[str]) -> Optional[float]:
    """Extracts the leading numeric component of a measurement string.

    "108kg" → 108.0, "85 kg" → 85.0, "12.5%" → 12.5, "1,200" → 1200.0.
    Returns None if no number is present.
    """
    if not value:
        return None
    m = _NUMERIC_RE.search(value.replace(",", ""))
    return float(m.group()) if m else None


def trajectory_note(
    goal_target: Optional[str],
    goal_by: Optional[date],
    measurements: list[Measurement],
    today: date,
) -> str:
    """
    Returns a one-line trend summary for a goal, or "" if there isn't enough to say
    anything useful (fewer than 2 numeric measurements, no numeric target, or no
    time elapsed between the first and last reading).

    Examples:
      "Trend: 108kg → 102kg over 21d (-0.29/day). On pace for 85kg by ~12 Mar 2026
       — ahead of the 1 Dec 2026 deadline."
      "Trend: 80kg → 82kg over 14d — moving away from the 85kg target."
      "Trend: flat at 100kg over 10d — no movement toward 85kg."
    """
    target = parse_measurement(goal_target)
    points = [m for m in measurements]
    if target is None or len(points) < 2:
        return ""

    points = sorted(points, key=lambda m: m.recorded_at)
    first, last = points[0], points[-1]
    span_days = (last.recorded_at - first.recorded_at).days
    if span_days <= 0:
        return ""

    delta = last.numeric - first.numeric
    rate = delta / span_days  # units per day
    head = f"Trend: {first.value} → {last.value} over {span_days}d"

    needed = target - last.numeric  # remaining distance to target (signed)
    if abs(needed) < 1e-9:
        return f"{head} — target {goal_target} reached."

    # Is the rate moving in the direction of the target?
    moving_toward = (needed > 0 and rate > 0) or (needed < 0 and rate < 0)

    if abs(rate) < 1e-9 or not moving_toward:
        if abs(rate) < 1e-9:
            return f"{head} — no movement toward {goal_target}."
        return f"{head} ({rate:+.2f}/day) — moving away from the {goal_target} target."

    days_to_target = needed / rate
    eta = today + _safe_timedelta_days(days_to_target)
    note = f"{head} ({rate:+.2f}/day). On pace for {goal_target} by ~{eta.strftime('%-d %b %Y')}"

    if goal_by is not None:
        slack = (goal_by - eta).days
        if slack >= 0:
            note += f" — ahead of the {goal_by.strftime('%-d %b %Y')} deadline."
        else:
            note += f" — behind the {goal_by.strftime('%-d %b %Y')} deadline by ~{-slack}d."
    else:
        note += "."
    return note


def _safe_timedelta_days(days: float) -> "timedelta":
    """Clamp a projected day-count to a sane range so eta arithmetic can't overflow."""
    from datetime import timedelta
    capped = max(0, min(int(round(days)), 365 * 50))
    return timedelta(days=capped)


def to_measurement(value: str, numeric: float, recorded_at: datetime) -> Measurement:
    """Constructor helper that normalises recorded_at to tz-aware UTC."""
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    return Measurement(value=value, numeric=numeric, recorded_at=recorded_at)


def points_from_rows(rows: list[dict]) -> list[Measurement]:
    """Builds numeric Measurements from memory.get_goal_measurements() rows."""
    return [
        to_measurement(r["value"], r["numeric"], r["recorded_at"])
        for r in rows
        if r["numeric"] is not None
    ]
