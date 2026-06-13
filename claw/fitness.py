"""
fitness.py

Responsibility: Fitness programme parsing, compliance tracking, and context building.

Reads a structured programme description from a Todoist task and produces
context blocks for injection into briefing and probe prompts. No SQLite —
the programme task description in Todoist is the single source of truth.
"""

from __future__ import annotations
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from claw.todoist_client import Task, TodoistClient

logger = logging.getLogger(__name__)

_DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Map Python weekday() (0=Mon) to day abbreviation
_PYTHON_DAY = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}

_WEEK_HEADER_RE = re.compile(
    r'^Week\s+(\d+)\s*\|\s*([^|]+?)\s*\|\s*Deload:\s*(Yes|No)', re.IGNORECASE
)
_DAY_LINE_RE = re.compile(
    r'^\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s*(?:\(([^)]+)\))?:\s*(.*)',
    re.IGNORECASE,
)
_LOG_ENTRY_RE = re.compile(
    r'^\[(\d{4}-\d{2}-\d{2})\s+\w+\s+W(\d+)\]\s*([✓✗—])\s*(.*)'
)
_CONSTRAINTS = {"office day", "flex day"}


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class DaySession:
    day: str
    session_type: Optional[str]   # e.g. "Strength A", "Cardio" — None for walk/rest days
    exercises: list[str]           # individual exercises, or [activity description] if no session type
    constraint: Optional[str]      # "office day", "flex day", or None
    is_deload: bool


@dataclass
class WeekPlan:
    week_num: int
    phase: str
    is_deload: bool
    sessions: dict[str, DaySession]  # keyed by day abbreviation e.g. "Mon"


@dataclass
class Programme:
    task_id: str
    name: str
    status: str           # "Active", "Complete", "Paused"
    start_date: date
    current_week: int
    notes: list[str]      # programme-wide rules (e.g. "Never train through joint pain")
    weeks: dict[int, WeekPlan]
    labels: list[str]
    log_lines: list[str] = field(default_factory=list)  # raw log section lines


@dataclass
class WeekCompliance:
    week_num: int
    completed: list[str]  # session types logged ✓ this week
    missed: list[str]     # session types logged ✗ this week
    unknown: list[str]    # session types scheduled but not yet logged


# ─── Parsing ─────────────────────────────────────────────────────────────────

def parse_programme(task) -> Programme:
    """
    Parses a programme task description into a Programme dataclass.
    Robust to minor formatting variation — unknown lines are silently skipped.
    """
    desc = task.description or ""
    lines = desc.splitlines()

    status = "Active"
    start_date_val = date.today()
    current_week = 1
    notes: list[str] = []
    weeks: dict[int, WeekPlan] = {}
    log_lines: list[str] = []

    state = "header"  # "header" | "notes" | "week" | "log"
    current_week_plan: Optional[WeekPlan] = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Log entries appear at column 0 in the description — check before indentation split
        if state == "log" and stripped.startswith("["):
            log_lines.append(stripped)
            continue

        # Non-indented lines are section headers or key: value pairs
        if not line[0].isspace():
            if stripped.lower() == "notes:":
                state = "notes"
                continue
            elif stripped.lower() == "log:":
                state = "log"
                current_week_plan = None
                continue

            m = _WEEK_HEADER_RE.match(stripped)
            if m:
                state = "week"
                w_num = int(m.group(1))
                w_phase = m.group(2).strip()
                w_deload = m.group(3).strip().lower() == "yes"
                current_week_plan = WeekPlan(
                    week_num=w_num, phase=w_phase,
                    is_deload=w_deload, sessions={},
                )
                weeks[w_num] = current_week_plan
                continue

            # key: value header field
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                key_lower = key.strip().lower()
                val_stripped = val.strip()
                if key_lower == "status":
                    status = val_stripped
                elif key_lower == "start":
                    try:
                        start_date_val = date.fromisoformat(val_stripped)
                    except ValueError:
                        pass
                elif key_lower == "current week":
                    try:
                        current_week = int(val_stripped)
                    except ValueError:
                        pass
            continue

        # Indented content — handled by current state
        if state == "notes":
            if stripped.startswith("-"):
                notes.append(stripped.lstrip("-").strip())
        elif state == "week" and current_week_plan is not None:
            m = _DAY_LINE_RE.match(line)
            if m:
                day = m.group(1).capitalize()
                session_type = m.group(2)  # None if no parens
                content = m.group(3).strip()
                exercises, constraint = _parse_day_content(content, session_type)
                current_week_plan.sessions[day] = DaySession(
                    day=day,
                    session_type=session_type,
                    exercises=exercises,
                    constraint=constraint,
                    is_deload=current_week_plan.is_deload,
                )

    return Programme(
        task_id=task.id,
        name=task.content,
        status=status,
        start_date=start_date_val,
        current_week=current_week,
        notes=notes,
        weeks=weeks,
        labels=task.labels,
        log_lines=log_lines,
    )


def _parse_day_content(text: str, session_type: Optional[str]) -> tuple[list[str], Optional[str]]:
    """Splits content into exercises and constraint keyword."""
    parts = [p.strip() for p in text.split(",")]
    constraint: Optional[str] = None
    items: list[str] = []
    for part in parts:
        if part.lower() in _CONSTRAINTS:
            constraint = part.lower()
        elif part:
            items.append(part)

    # No session type → single activity description, not a list of exercises
    if not session_type and items:
        return [", ".join(items)], constraint
    return items, constraint


# ─── Lookup helpers ───────────────────────────────────────────────────────────

def get_active_programme(programme_tasks: list) -> Optional[Programme]:
    """Returns the first task with Status: Active, parsed as a Programme."""
    for task in programme_tasks:
        try:
            prog = parse_programme(task)
            if prog.status.lower() == "active":
                return prog
        except Exception as e:
            logger.warning(f"Failed to parse programme task {task.id}: {e}")
    return None


def get_today_session(programme: Programme, today: date) -> Optional[DaySession]:
    """Returns today's DaySession from the current week's plan, or None."""
    week_plan = programme.weeks.get(programme.current_week)
    if not week_plan:
        return None
    day_abbr = _PYTHON_DAY[today.weekday()]
    return week_plan.sessions.get(day_abbr)


def get_week_compliance(programme: Programme) -> WeekCompliance:
    """
    Reads the Log: section of the programme task to determine what was
    completed, missed, or not yet logged in the current week.
    """
    week_plan = programme.weeks.get(programme.current_week)
    scheduled_types: set[str] = set()
    if week_plan:
        for s in week_plan.sessions.values():
            if s.session_type:
                scheduled_types.add(s.session_type)

    completed: list[str] = []
    missed: list[str] = []

    for line in programme.log_lines:
        m = _LOG_ENTRY_RE.match(line)
        if not m:
            continue
        if int(m.group(2)) != programme.current_week:
            continue
        symbol = m.group(3)
        rest = m.group(4).strip()
        # Session type is everything before " — " or the first word cluster
        session_type = rest.split("—")[0].strip() if "—" in rest else rest.split(",")[0].strip()
        if symbol == "✓":
            completed.append(session_type)
        elif symbol == "✗":
            missed.append(session_type)

    logged = set(completed) | set(missed)
    unknown = sorted(scheduled_types - logged)

    return WeekCompliance(
        week_num=programme.current_week,
        completed=completed,
        missed=missed,
        unknown=unknown,
    )


def compliance_urgency(compliance: WeekCompliance) -> str:
    """Returns "normal" / "flagged" (2 missed) / "urgent" (3+ missed)."""
    n = len(compliance.missed)
    if n >= 3:
        return "urgent"
    if n >= 2:
        return "flagged"
    return "normal"


def get_last_log_date(programme: Programme) -> Optional[date]:
    """Returns the date of the most recent log entry, or None if the log is empty."""
    last: Optional[date] = None
    for line in programme.log_lines:
        m = _LOG_ENTRY_RE.match(line.strip())
        if m:
            try:
                d = date.fromisoformat(m.group(1))
                if last is None or d > last:
                    last = d
            except ValueError:
                pass
    return last


# ─── Week advancement ─────────────────────────────────────────────────────────

def should_advance_week(programme: Programme, today: date) -> bool:
    """True if it's Monday and the calendar week count exceeds current_week."""
    if today.weekday() != 0:  # not Monday
        return False
    days_elapsed = (today - programme.start_date).days
    calendar_week = days_elapsed // 7 + 1
    return calendar_week > programme.current_week


def advance_week(programme: Programme, todoist) -> int:
    """Updates Current Week: N in the Todoist task description. Returns new week number."""
    from claw.todoist_client import _update_description_field
    new_week = programme.current_week + 1
    resp = todoist._request_with_retry(
        "GET", f"{todoist.BASE_URL}/tasks/{programme.task_id}"
    )
    current_desc = resp.json().get("description", "") or ""
    new_desc = _update_description_field(current_desc, "Current Week", str(new_week))
    todoist.update_task_description(programme.task_id, new_desc)
    logger.info(f"Advanced '{programme.name}' to Week {new_week}")
    return new_week


# ─── Programme log ────────────────────────────────────────────────────────────

def append_programme_log(programme: Programme, entry: str, todoist) -> None:
    """Appends one line to the Log: section of the programme task description."""
    try:
        todoist.append_to_task_description(programme.task_id, f"\n{entry}")
        logger.info(f"Appended to programme log: {entry}")
    except Exception as e:
        logger.warning(f"Failed to append programme log: {e}")


# ─── Weather ─────────────────────────────────────────────────────────────────

def get_weather_london() -> Optional[str]:
    """
    Fetches current London weather via wttr.in (no API key).
    Returns e.g. "Heavy rain +12°C" or None on any failure.
    """
    try:
        url = "http://wttr.in/London?format=%25C+%25t"
        req = urllib.request.Request(url, headers={"User-Agent": "claw/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode().strip()
        if len(text) > 80 or "<" in text:
            logger.warning(f"Unexpected weather response (possible HTML): {text[:80]!r}")
            return None
        return text
    except Exception as e:
        logger.warning(f"Weather fetch failed: {e}")
        return None


# ─── Context builders ─────────────────────────────────────────────────────────

def build_fitness_briefing_context(
    programme: Programme, compliance: WeekCompliance, today: date
) -> str:
    """
    Returns a structured context block for injection into BRIEFING_USER_TEMPLATE.
    Data-heavy, not prose — Claude writes the narrative from this.
    """
    week_plan = programme.weeks.get(programme.current_week)
    today_day = _PYTHON_DAY[today.weekday()]
    lines = [
        f"Fitness programme: {programme.name}",
        f"Week {programme.current_week} of 13 | Phase: {week_plan.phase if week_plan else 'unknown'}"
        + (" | DELOAD WEEK" if week_plan and week_plan.is_deload else ""),
    ]

    today_session = get_today_session(programme, today)
    if today_session:
        if today_session.session_type:
            ex = ", ".join(today_session.exercises) if today_session.exercises else "no exercises listed"
            constraint = f" | {today_session.constraint}" if today_session.constraint else ""
            lines.append(f"Today ({today_day}) — {today_session.session_type}: {ex}{constraint}")
        else:
            activity = today_session.exercises[0] if today_session.exercises else "rest"
            constraint = f" ({today_session.constraint})" if today_session.constraint else ""
            lines.append(f"Today ({today_day}) — {activity}{constraint}")
    else:
        lines.append(f"Today ({today_day}) — no session scheduled")

    if compliance.completed:
        lines.append(f"Completed this week: ✓ {', '.join(compliance.completed)}")
    if compliance.missed:
        lines.append(f"Missed this week: ✗ {', '.join(compliance.missed)}")
        remaining = _remaining_days(programme, today)
        if remaining:
            lines.append(f"Days still available: {', '.join(remaining)}")
    if compliance.unknown:
        lines.append(f"Not yet logged this week: {', '.join(compliance.unknown)}")

    if programme.notes:
        lines.append("Programme rules: " + "; ".join(programme.notes))

    return "\n".join(lines)


def build_fitness_probe_context(
    programme: Programme,
    today_session: Optional[DaySession],
    compliance: WeekCompliance,
    task,
) -> str:
    """
    Returns a structured context block for injection into PROBE_USER_TEMPLATE
    when the probed task is a fitness habit.
    """
    week_plan = programme.weeks.get(programme.current_week)
    lines = [
        f"Programme: {programme.name}",
        f"Week {programme.current_week} | Phase: {week_plan.phase if week_plan else 'unknown'}"
        + (" | DELOAD WEEK" if week_plan and week_plan.is_deload else ""),
    ]

    if today_session and today_session.session_type:
        ex = ", ".join(today_session.exercises) if today_session.exercises else "no exercises listed"
        lines.append(f"Today's session: {today_session.session_type} — {ex}")
    elif today_session:
        activity = today_session.exercises[0] if today_session.exercises else "rest day"
        lines.append(f"Today: {activity}")
    else:
        lines.append("Today: no session scheduled in this week's plan")

    if compliance.completed:
        lines.append(f"Completed this week: {', '.join(compliance.completed)}")
    if compliance.missed:
        lines.append(f"Missed this week: {', '.join(compliance.missed)}")

    if programme.notes:
        lines.append("Programme rules (non-negotiable):")
        for note in programme.notes:
            lines.append(f"  - {note}")

    # Weather context for cardio sessions
    if today_session and today_session.session_type and \
            "cardio" in today_session.session_type.lower():
        weather = get_weather_london()
        if weather:
            lines.append(f"Weather (London, now): {weather}")
            lines.append(
                "Routing: heavy rain or dark → Concept 2; "
                "light rain/cloudy → ask user; clear → assume cycling"
            )

    return "\n".join(lines)


def _remaining_days(programme: Programme, today: date) -> list[str]:
    """Returns descriptive labels for days remaining in the current week after today."""
    week_plan = programme.weeks.get(programme.current_week)
    if not week_plan:
        return []
    today_idx = today.weekday()
    remaining = []
    for day in _DAY_ORDER:
        day_idx = _DAY_ORDER.index(day)
        if day_idx <= today_idx:
            continue
        session = week_plan.sessions.get(day)
        if not session:
            continue
        label = day
        if session.session_type:
            label += f" ({session.session_type})"
        if session.constraint:
            label += f" [{session.constraint}]"
        remaining.append(label)
    return remaining
