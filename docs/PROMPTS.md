# Prompt Design Notes

This document explains the intent behind each prompt in `claw/prompts.py`, records changes, and captures what was learned from testing. It is the companion to the code, not a replacement for reading it.

---

## Design Philosophy

Claw's character is defined entirely in prompts. The Python code is plumbing. The prompt is the person.

A few principles that should survive any prompt revision:

**One thing at a time.** The system must never generate a list of demands. If it surfaces multiple tasks in a briefing, they should feel like awareness, not assignments. If it probes, it probes one thing.

**Questions over statements.** "This has been sitting for 8 days" is an observation. "Why has this been sitting for 8 days — is something blocking it?" is a conversation. The second is what Claw should do.

**Memory makes it feel human.** When Claude references something from last week, the interaction shifts from transactional to relational. Prompts must always include relevant memory context.

**Earn the right to push.** If engagement is low (user is giving one-word answers, hasn't responded), back off. Don't escalate. The prompt should encode this explicitly.

---

## Prompts

### BRIEFING_SYSTEM

**Purpose:** Sets Claude's character and role for the morning briefing session.

**Intent:** Claude should read the task list and produce something that feels like a thoughtful friend summarising the day — not a PM tool listing tickets. The briefing should give a *sense* of the day (light vs heavy, one clear thing vs scattered), not enumerate everything.

**Key instructions the prompt must encode:**
- Do not list more than 3-4 tasks; pick the most meaningful ones
- Acknowledge if the day looks heavy without catastrophising it
- Reference any relevant memory context (e.g. "you were going to finish X today — still on?")
- End with one light, open invitation rather than a call to action

**Version history:**
- v1 (Phase 1): Initial prompt — TBD

---

### PROBE_SYSTEM

**Purpose:** Sets Claude's character for a probe conversation about a single stuck task.

**Intent:** Claude has been handed one task and its history. It should open a conversation that feels natural — not a status update request, not a ticketing system. More like a friend who noticed something and is curious about it.

**Key instructions the prompt must encode:**
- You are talking about ONE task only. Do not introduce others.
- Ask one question. Not three.
- The question should be genuinely curious, not performatively concerned
- If memory shows this has been discussed before, reference it naturally
- Offer concrete options if the user seems stuck: reschedule, break it down, kill it
- If the user seems disengaged (short answers, "yeah whatever"), offer to drop it

**Tone guidance:**
- Default: warm, direct, a little dry
- If memory shows low recent engagement: shorter, softer, give them an out
- If memory shows high engagement: can be more probing, more playful

**Version history:**
- v1 (Phase 1): Initial prompt — TBD

---

### PROBE_FOLLOWUP

**Purpose:** Continues a probe conversation after the user has replied.

**Intent:** The first probe message opens the conversation. This prompt handles the continuation — absorbing what the user said and either helping them take a next step, or gracefully closing the conversation.

**Key instructions the prompt must encode:**
- Read what the user said carefully before responding
- If they've identified a blocker: acknowledge it, ask what would unblock it (one question)
- If they want to reschedule: do it without judgement, confirm the new date
- If they want to kill the task: affirm the decision, ask if there's anything to capture before closing it
- If they've said they'll just do it: great, close warmly, don't drag it out
- If the reply is disengaged: don't push, close the loop gently

**Version history:**
- v1 (Phase 1): Initial prompt — TBD

---

### TASK_SELECTION_SYSTEM

**Purpose:** Used in a separate Claude call to choose which task to probe.

**Intent:** Given the full task list and memory context, Claude picks one task to probe and explains why. This output is parsed, not sent to the user.

**Key instructions the prompt must encode:**
- Output must be JSON: `{"task_id": "...", "reason": "..."}`
- Reason is for logging only — it will not be shown to the user
- Prefer tasks that have been stuck for a while and not recently discussed
- Avoid tasks that were explicitly rescheduled and haven't hit their new date yet
- Avoid probing the same task two days in a row
- If nothing warrants a probe, return `{"task_id": null, "reason": "..."}`

**Version history:**
- v1 (Phase 1): Initial prompt — TBD

---

## Changelog

### Phase 1.9 (16 May 2026)
- `LISTENER_INTENT_SYSTEM` — added `"probe"` intent: user can trigger an on-demand probe session by saying "probe me", "check in with me", "what should I work on?". Listener routes to `run_probe()` directly.

### Phase 2 (19 May 2026)
- `BRIEFING_SYSTEM` — restructured to lead with goals, not tasks. The most active goal and the task that advances it are the opening frame. QUIET goals (7+ days no activity) surface prominently. Secondary tasks get one collective line.
- `BRIEFING_USER_TEMPLATE` — `{goal_context}` moved to the top so Claude reads goals before tasks.
- `PROBE_SYSTEM` — goal is now the opening frame: "You're working toward X. This task is your current path there." Gap framing (current → target) promoted to a primary instruction.
- `PROBE_USER_TEMPLATE` — `{goal_line}` moved before task details.
- `TASK_SELECTION_SYSTEM` — goal weighting changed from tiebreaker to active preference: "Explicitly prefer goal-linked tasks. Only fall back to non-goal tasks if none are eligible." QUIET goal preference and 90-day deadline urgency weighting added.
- `PROBE_FOLLOWUP_SYSTEM` — added: acknowledge concrete goal progress in follow-ups (one genuine line; silence is also fine).

### Phase 3 (19 May 2026)
- `BRIEFING_SYSTEM` — QUIET goal flag (`← QUIET`) explained; concrete gap framing with current → target values.
- `TASK_SELECTION_SYSTEM` — deadline urgency: weight tasks from goals with `By` within 90 days higher.
- `GOAL_UPDATE_DETECTION_SYSTEM` — new prompt. Detects explicit measurements from probe conversations ("I weighed 107kg") and triggers a write-back to the goal's `Current:` field. Strict: no inference, no estimates, no vague language.

### Phase 4 (20 May 2026)
- `LISTENER_INTENT_SYSTEM` — no change to the prompt itself; M-code replies now bypass it entirely via a regex fast-path before the Claude call.
- `TASK_SELECTION_USER_TEMPLATE` — `{goal_context}` and `{previous_topic}` placeholders added (these were wired up in Phase 2/3 but the template reference hadn't been noted here).

### Phase 6 — usefulness (14 June 2026)
- `LISTENER_INTENT_SYSTEM` — added the `"capture"` intent (add a task/reminder).
- `CAPTURE_EXTRACTION_SYSTEM` — new. Extracts {content, project, section} from a capture
  message; defaults to home/TODAY when ambiguous.
- `WEEKLY_REVIEW_SYSTEM` / `WEEKLY_REVIEW_USER_TEMPLATE` — new. The Sunday reflection:
  what moved, what stalled, goal trajectory, one question ahead.
- `PROBE_ACTION_BUTTONS` / `PROBE_ACTION_REPLIES` — inline-button labels and the
  callback_data → reply-text map. A tap is treated as sending the mapped text, so the
  existing detectors handle it. `MSG_TASK_CAPTURED` / `MSG_TASK_CAPTURE_FAILED` added.

### Cleanup (14 June 2026)
- Added `MSG_*` **fixed-message** constants — short static lines Claw sends directly
  (not model output): `MSG_PROBE_ALL_CLEAR`, `MSG_PROBE_TIMEOUT_FALLBACK`,
  `MSG_BRIEFING_EMPTY`, `MSG_CODES_CLOSED`/`MSG_CODES_UNKNOWN` (M-code acks),
  `MSG_FREEFORM_LOGGED`. Moved out of `probe.py`/`listener.py`/`briefing.py` so Claw's
  voice is consistent and overridable via `config/prompts.yaml` like the prompts.
  Templates use `{named}` fields filled by the caller.
