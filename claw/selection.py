"""
selection.py

Responsibility: Ask Claude (cheap model) to pick the single task worth probing,
and format tasks compactly for that decision.

Extracted from probe.py to keep the probe orchestration focused. This module is a
leaf: it depends on the Claude client and prompts, never back on probe.py.
"""

from __future__ import annotations
import json
import logging
import re
from typing import Optional

from claw.claude_client import ClaudeClient
from claw.memory import MemoryStore, TaskMemory
from claw.todoist_client import Task
from claw import prompts

logger = logging.getLogger(__name__)

_strip_json_fences = prompts.strip_json_fences

_TASK_ID_RE = re.compile(r'"task_id"\s*:\s*"([^"]+)"')
_NULL_TASK_RE = re.compile(r'"task_id"\s*:\s*null')


def _extract_partial_selection(raw: str) -> Optional[dict]:
    """Extracts task_id from a truncated JSON response using regex fallback.

    Takes the LAST occurrence of each pattern so that a task_id mentioned inside
    a 'reason' string value doesn't shadow the real root-level task_id key.
    """
    null_positions = [m.start() for m in _NULL_TASK_RE.finditer(raw)]
    id_matches = list(_TASK_ID_RE.finditer(raw))

    if not null_positions and not id_matches:
        return None

    last_null = null_positions[-1] if null_positions else -1
    last_id = id_matches[-1] if id_matches else None
    last_id_pos = last_id.start() if last_id else -1

    logger.warning(f"Task selection JSON truncated — regex fallback: {raw!r}")
    if last_null > last_id_pos:
        return {"task_id": None}
    return {"task_id": last_id.group(1)}


def _select_task(
    tasks: list[Task],
    memory: MemoryStore,
    claude: ClaudeClient,
    config: dict,
    last_discussed: Optional[Task] = None,
    goal_context: str = "",
    fitness_urgency: str = "normal",
) -> Optional[Task]:
    """
    Asks Claude (cheap model) to pick one task to probe. Returns the Task or None.
    """
    task_list_with_memory = "\n".join(
        _format_task_for_selection(t, memory.get_task_memory(t.id))
        for t in tasks
    )
    previous_topic = last_discussed.content if last_discussed else ""
    if fitness_urgency == "urgent":
        fitness_urgency_note = (
            "\nCOMPLIANCE FLAG: 3+ fitness sessions missed this week. "
            "Prioritise fitness habits above work tasks in this session."
        )
    else:
        fitness_urgency_note = ""
    raw = claude.complete(
        system=prompts.get_prompt("TASK_SELECTION_SYSTEM"),
        user=prompts.TASK_SELECTION_USER_TEMPLATE.format(
            task_list_with_memory=task_list_with_memory,
            goal_context=goal_context or "No goals configured.",
            previous_topic=previous_topic,
            fitness_urgency_note=fitness_urgency_note,
        ),
        max_tokens=config["claude"]["selection_max_tokens"],
        model=config["claude"]["selection_model"],
    )

    try:
        parsed = json.loads(_strip_json_fences(raw))
    except json.JSONDecodeError:
        parsed = _extract_partial_selection(raw)
        if parsed is None:
            logger.warning(f"Task selection returned non-JSON: {raw!r}")
            return None

    selected_id = parsed.get("task_id")
    if not selected_id:
        logger.info(f"Task selection: no probe needed — {parsed.get('reason', '')}")
        return None

    task_map = {t.id: t for t in tasks}
    if selected_id not in task_map:
        logger.warning(f"Task selection returned unknown id: {selected_id!r}")
        return None

    logger.info(f"Selected task {selected_id}: {parsed.get('reason', '')}")
    return task_map[selected_id]


def _format_task_for_selection(task: Task, task_memory: Optional[TaskMemory]) -> str:
    """
    Compact one-liner for the task selection prompt.
    Claude needs just enough to make a good choice.
    """
    habit_tag = " [HABIT]" if task.is_habit else (" [WAITING]" if task.is_waiting else "")
    overdue = f", overdue {task.days_overdue}d" if task.is_overdue else ""
    from claw.memory import _days_ago
    if task_memory and task_memory.last_probed_at:
        age = _days_ago(task_memory.last_probed_at)
        memory_str = f", last probed {age}d ago, outcome: {task_memory.last_outcome or 'unknown'}"
    else:
        memory_str = ", never probed"

    snoozed = ""
    if task_memory and task_memory.snoozed_until:
        snoozed = f", SNOOZED until {task_memory.snoozed_until.date()}"

    return (
        f"- task_id: {task.id},{habit_tag} [{task.section_name}] {task.content} "
        f"({task.project_name}){overdue}{memory_str}{snoozed}"
    )
