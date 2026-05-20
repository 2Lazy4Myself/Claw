# Build Log

This document is the living record of what was built, when, why, and what was learned. It is updated at the end of every phase or significant change. It is not a changelog — it is a narrative.

---

## Phase 1 — MVP

**Goal:** A working, deployable system that does the two core things: sends a morning briefing and can open a probe conversation about one stuck task. Memory is real but simple. Tone is right. Nothing is hardcoded.

**Status:** ✅ Complete — deployed to Unraid, 16 May 2026

**Scope:**

- [x] `todoist_client.py` — section-aware task fetching (sections as temporal signal, not due dates); Todoist API v1 with cursor pagination
- [x] `memory.py` — SQLite store for per-task probe history and session log; `build_context_block()` pure function for prompt injection
- [x] `claude_client.py` — Anthropic API wrapper with retry on rate limit/connection errors; optional model override for cheap vs. powerful calls
- [x] `prompts.py` — BRIEFING_SYSTEM, PROBE_SYSTEM, PROBE_FOLLOWUP, TASK_SELECTION prompts (v1); YAML override support for local tuning
- [x] `telegram_client.py` — send message, long-polling reply listener with drain to avoid stale messages
- [x] `briefing.py` — orchestrates morning summary; all projects, all today/overdue tasks, Claude writes the message
- [x] `probe.py` — picks one task via cheap model (JSON selection), opens conversation with Sonnet, multi-turn loop, session logged with summary
- [x] Unit tests: 35 passing, covering task model, memory context, prompt loading, config validation, all format functions, close detection
- [x] Integration tests: Todoist fetch, Claude completion, memory roundtrip (tagged, excluded from default run)
- [x] `config/config.example.yaml` — fully documented, dual model config (conversation vs. selection)
- [x] `.env.example` — documented secrets template
- [x] `scripts/run_briefing.sh` and `scripts/run_probe.sh`
- [x] Deployed to Unraid as Docker container (Alpine + crond), cron at 08:00 and 18:00 Europe/London
- [x] `listener.py` — not built; not needed for MVP. Probe session handles replies within its own polling window.

**Key decisions made:**

- SQLite for memory (atomic writes, queryable — JSON files have race condition risk)
- Long-polling for Telegram (no public HTTPS endpoint on home network)
- Multi-turn probe loop in `probe.py` itself (no separate listener process for MVP)
- Two model tiers: `claude-sonnet-4-6` for conversation, `claude-haiku-4-5-20251001` for selection + summarisation
- Sections as temporal signal — Jake's Todoist uses sections (Today / Next 2-3 Days / etc.) as the "when", not native due dates (ADR-006)

**What was learned:**

- Haiku sometimes wraps JSON in markdown code fences despite explicit instructions — strip before parsing (now handled in `_select_task` and `_write_habit_log`)
- `datetime.now()` vs `datetime.now(timezone.utc)` — naive datetimes cause off-by-one in day calculations when system timezone ≠ UTC; always use UTC-aware datetimes in tests
- The conversation close heuristic (no question mark + short response) works well in practice; extension seam left for Phase 4 JSON-based detection

---

## Phase 1.5 — Lifestyle Habit Tracking

**Goal:** Extend the probe pool to include ongoing lifestyle habits from a dedicated Todoist project. Habits compete equally with tasks. Claw writes a timestamped log back to the Todoist task description after every habit probe.

**Status:** ✅ Complete — deployed 16 May 2026

**What was built:**

- `HABIT_SECTIONS` — set of Todoist section IDs that mark a task as a lifestyle habit
- `get_lifestyle_habits()` — fetches tasks from Claw/Life Style section; these are always in scope (no temporal filtering)
- `update_task_description()` — Todoist API write-back; appends log entries to task description
- `is_habit: bool` field on `Task` dataclass
- `_write_habit_log()` in `probe.py` — after every habit probe, cheap model generates a structured log line (✓/✗/—) plus optional extended note if something meaningful happened
- `HABIT_LOG_SYSTEM` prompt — returns JSON `{"log": "...", "note": "..."}`
- Updated `PROBE_SYSTEM` — habit-specific instructions: build vs. stop habits handled differently; early evening weighting for motivational habits; reference log history
- Updated `TASK_SELECTION_SYSTEM` — habit-awareness; prioritise habits with ✗ streaks; weight motivational habits at 18:00

**Habits currently tracked (Claw/Life Style section):**

| Habit | Context |
|---|---|
| Strength Training | Cholesterol + arthritis barriers. Subtask: Find Resistance Bands |
| Get on top of boozing | Reducing from ~100 units/week. 4 days sober at time of first probe |

**Key decisions:**

- Habits compete equally with tasks in the same probe slot — Claude picks whichever is most worth discussing
- Always write a log line after a habit probe (even no_reply); only write an extended note if something meaningful was said
- Log lives in Todoist task description (not just SQLite) so the history is visible and editable in Todoist itself
- Swinburne section excluded for now

**What was learned:**

- Haiku immediately understood the habit/task distinction and cited "early evening is high-value timing for this type of habit check-in" on first run — the prompt framing worked
- The running log in Todoist description (`[16 May] — Acknowledged 4 days sober, DTs avoided`) gives Claude a human-readable history without needing to query the DB for habit-specific context

---

## Phase 1.6 — Todoist Write-Back: Task and Subtask Closing

**Goal:** When a user confirms task completion during a probe, Claw closes it in Todoist automatically and sends a Telegram confirmation. No more manual ticking.

**Status:** ✅ Complete — 16 May 2026

**What was built:**

- `close_task(task_id)` on `TodoistClient` — `POST /tasks/{id}/close` (204 No Content)
- `get_subtasks(task_id)` on `TodoistClient` — fetches active subtasks by `parent_id`
- `COMPLETION_DETECTION_SYSTEM` prompt — cheap model reads the conversation and returns JSON: `{"action": "close_task|close_subtask|none", "subtask_name": "..."}`
- `_detect_and_close()` in `probe.py` — runs after every non-`no_reply` probe; guards habits from being closed (they're ongoing); sends `✓ Checked off in Todoist.` or `✓ Checked off 'subtask name' in Todoist.`
- `_find_subtask()` — pure function, case-insensitive exact then partial name match; 5 unit tests

**Live test result (first run):**

Probed "Strength Training" habit. User mentioned the resistance bands were ready. Result:
- Habit log appended: `[16 May] ✓ planned resistance band exercises — Starting with chair/standing/floor exercises; bands already sourced`
- Subtask "Find Resistance Bands" automatically closed in Todoist
- Telegram confirmation sent: `✓ Checked off 'Find Resistance Bands' in Todoist.`

**Key decisions:**

- Detection runs post-conversation (not inside the loop) — cleaner, full transcript available
- Habits are explicitly guarded: `close_task` never fires if `is_habit=True`; subtask closing still works for habits
- Subtask name matching is fuzzy (exact first, partial fallback) — avoids brittleness when Claude paraphrases the subtask name

---

## Phase 1.7 — Inbound Listener + Snooze

**Goal:** Make Claw two-way. Any message you send outside a probe session gets a response. If you defer a task during a probe, Claw stops asking about it until the date you named.

**Status:** ✅ Complete — 16 May 2026

**What was built:**

- `listener.py` — cron job (every 2 min) that processes inbound Telegram messages outside of probe sessions. Classifies intent (`briefing` | `general`) with the cheap model and routes accordingly. Exits immediately if the probe lock file exists.
- `PROBE_LOCK_FILE` (`/tmp/claw_probe.lock`) — probe creates this at startup, removes it on exit. Listener bails if it's present, preventing both processes from consuming the same Telegram updates.
- `listener_state` SQLite table — stores the Telegram update offset so each cron run picks up where the last left off. No message is processed twice, no message is lost between runs.
- `get_listener_offset() / set_listener_offset()` on `MemoryStore`
- `TelegramClient.get_updates()` — public, non-blocking fetch used by the listener
- `LISTENER_INTENT_SYSTEM` prompt — classifies inbound messages
- `LISTENER_GENERAL_SYSTEM` prompt — Claude responds with session context for general messages
- Snooze detection in `probe.py`: `_detect_and_snooze()` runs post-conversation (same pattern as `_detect_and_close`). Asks cheap model for a date, writes `snoozed_until` to `TaskMemory`, sends a Telegram confirmation. The final `upsert_task_memory` uses the newly detected snooze date.
- `SNOOZE_DETECTION_SYSTEM` prompt — returns JSON `{"snooze": bool, "date_iso": "YYYY-MM-DD"}`; today's date injected for relative-date resolution
- `_is_snoozed()` in `probe.py` — pre-filters snoozed tasks before Claude even sees the candidate list
- 6 new unit tests: `_is_snoozed` (3 cases), listener offset roundtrip (3 cases)
- `docker/crontab` updated: `*/2 * * * *` listener entry added
- `scripts/run_listener.sh` added

**Key decisions:**

- Listener as cron job (not a daemon) — simpler deployment, no process supervision needed, no socket/IPC required
- Lock file for coordination — probe creates it at the top of `run_probe`, removes in `finally`. Listener checks and exits if present. This is the simplest form of mutual exclusion that works in a single-container environment.
- Offset in SQLite (not a file) — consistent with everything else in memory; atomic writes, no race on the file system
- Snooze detection post-conversation — full transcript available, same clean pattern as completion detection
- `_is_snoozed()` as a pre-filter in `_run_probe_inner` — cheaper and more reliable than trusting the selection prompt alone

**What was learned:**

- The lock file + cron approach avoids all the complexity of shared Telegram offset management between concurrent processes. The probe simply owns polling during its window; the listener owns it the rest of the time.
- Relative date resolution ("Thursday") requires today's date injected into the snooze prompt — Claude can't compute relative dates without knowing when "now" is.

---

## Tidy — Cross-module coupling cleanup

**Status:** ✅ Complete — 16 May 2026

**What changed:**

- `_strip_json_fences` moved from `probe.py` to `prompts.strip_json_fences` (public). Its natural home is the module that handles Claude output. Both `probe.py` and `listener.py` already import `prompts`, so no new dependencies needed. A one-line alias in `probe.py` keeps all internal call sites unchanged.
- `PROBE_LOCK_FILE` duplicated into `listener.py` as a local constant. Removed the import from `probe.py`. A path string doesn't warrant a cross-module dependency.
- `import os` ordering fixed in `listener.py` (stdlib before local imports).

---

## Phase 1.8 — Habits in Morning Briefing

**Goal:** Habits appear in the morning briefing so the daily picture is complete, not just tasks.

**Status:** ✅ Complete — 16 May 2026

**What was built:**

- `get_lifestyle_habits()` called in `run_briefing` alongside task fetching
- `_format_habits_for_prompt()` — formats each habit as `- Name: <last log line>` (or `no log yet` if description is empty)
- `_last_log_line()` — extracts the last non-empty line from a habit's Todoist description, which holds the running probe log
- `BRIEFING_USER_TEMPLATE` gains a `Lifestyle habits:` block between tasks and memory context
- `BRIEFING_SYSTEM` gains one instruction: weave in a brief mention of struggling or unlogged habits — don't list them all

**Key decisions:**

- Last log line is the right signal: it's the most recent state, already human-readable, no extra DB query
- Claude decides whether/how to mention it — the instruction says "one is enough", not "list all"
- No new API calls, no schema changes — purely additive to the briefing prompt

---

## Phase 1.9 — Constant Cleaning + Waiting For + On-Demand Probe

**Goal:** Make Claw more fluid. Rather than one nudge per session, keep going while the user is engaged. Surface "Waiting For" tasks that were invisible to Claw. Let the user trigger a probe at any time, not just at 08:00 and 18:00.

**Status:** ✅ Complete — 16 May 2026

**What was built:**

**Session chaining (Constant Cleaning):**
- `_run_probe_inner` became a loop (up to `max_chain_length`, default 5)
- `_probe_one_task()` extracted — handles one task end-to-end and returns outcome
- Loop exits immediately on `no_reply`; continues to next topic on `closed` or `max_turns_reached`
- `discussed_ids` set grows each iteration; already-covered tasks excluded from selection
- `last_discussed` task passed to `_select_task()` as `previous_topic` — selection prompt weights thematically related next items higher
- `chain_context` injected into `PROBE_USER_TEMPLATE` when `chain_index > 0` — Claude opens the next topic naturally without recapping

**Waiting For tracking:**
- `WAITING_SECTIONS` set added to `todoist_client.py` (derived from PROJECTS dict, covers work + home)
- `Task.is_waiting: bool` field; set in `_parse()` when section_id is in WAITING_SECTIONS
- Waiting tasks fetched via `get_waiting_for()` and added to the probe pool
- Separate staleness threshold: `waiting_for_min_probe_hours` (default 72h vs 48h for regular tasks)
- Tagged `[WAITING]` in selection and `Type: WAITING FOR` in probe — prompts use "did this come through?" framing
- Morning briefing includes waiting count + oldest overdue item
- `_detect_and_close()` already handles close on "got it" — no changes needed

**On-demand probe:**
- `LISTENER_INTENT_SYSTEM` gains `"probe"` intent
- `_handle_probe()` in `listener.py` calls `run_probe()` directly
- Lock file coordination unchanged — probe acquires lock, other listener runs bail

**Refactor (post-build tidy):**
- `started_at` removed from `_run_probe_inner` signature (each `_probe_one_task` computes its own)
- `LISTENER_INTENT_SYSTEM` JSON schema updated to `"briefing" | "probe" | "general"`

**Config additions:**
```yaml
behaviour:
  max_chain_length: 5
  waiting_for_min_probe_hours: 72
```

**Tests:** 10 new unit tests (56 total): `TestIsWaiting` (6), `TestFormatWaitingForPrompt` (4)

**Key decisions:**
- `all_tasks` fetched once before the chaining loop — no re-fetching between topics; task availability is stable within a session
- Waiting tasks use a separate (longer) staleness threshold, computed before the loop via a second `get_tasks_not_recently_probed()` call
- On-demand probe via listener (not a midday cron) — user controls when, variable timing without Phase 2's engagement model
- Proactive Claw-initiated probes at arbitrary hours deferred to Phase 2

---

## Phase 2 — Adaptive Cadence + Goal-First Psychology

**Goal:** Replace fixed twice-daily cron with a flexible orchestrator that checks in whenever there's something worth saying — up to once every 90 minutes during the active window. Also make goals the motivating spine of every conversation, not appended metadata.

**Status:** ✅ Complete — 19 May 2026

**Part A — Orchestrator (cadence):**

The two fixed cron entries (08:00 briefing, 18:00 probe) are replaced by a single `*/30 * * * *` job that runs `claw.orchestrator`. On each tick, the orchestrator decides: brief, probe, or stay silent.

Decision logic:
1. Outside active window (07:00–21:00 London)? → exit silently
2. Within morning window (07:00–10:00) AND no briefing logged today? → `run_briefing()`
3. Less than 90 minutes since last session? → exit silently
4. Otherwise → `run_probe()`

New files and changes:
- `claw/orchestrator.py` — orchestration logic; pure decision functions `_within_active_window`, `_briefing_window_open`, `_briefing_sent_today`, `_minutes_since_last_session`
- `claw/memory.py` — two new query methods: `get_last_session_at()`, `get_last_briefing_date()`
- `docker/crontab` — replaced two entries with single `*/30 * * * *` orchestrator entry
- `config/config.yaml` — replaced `briefing_time`/`probe_time` with `active_window_start`, `active_window_end`, `briefing_window_end`, `min_minutes_between_sessions`
- `claw/config.py` — schedule keys added to required validation list
- `tests/unit/test_orchestrator.py` — 16 new unit tests for decision functions

**Part B — Goal-first psychology (prompts):**

Goals were present as data but not as the motivating spine of conversations. The probe opened with the task; the briefing listed tasks and mentioned goals as an afterthought; task selection treated goals as a tiebreaker.

Changes — all in `claw/prompts.py`:
- `BRIEFING_SYSTEM` — restructured: lead with the most active goal and the single task that advances it today. Secondary tasks get one collective line. QUIET goals surface prominently, not as afterthought.
- `BRIEFING_USER_TEMPLATE` — `{goal_context}` moved to top of template so Claude reads goals before tasks.
- `PROBE_SYSTEM` — goal is now the opening frame, not optional context: "You're working toward X. This task is your current path there." Gap framing (current → target) is a primary instruction, not a buried rule.
- `PROBE_USER_TEMPLATE` — `{goal_line}` moved before task details so Claude reads WHY before WHAT.
- `TASK_SELECTION_SYSTEM` — goal weighting changed from tiebreaker to active preference: "Explicitly prefer goal-linked tasks. Only fall back to non-goal tasks if none are eligible."
- `PROBE_FOLLOWUP_SYSTEM` — added: acknowledge concrete goal progress in follow-ups when genuine.

**Config additions:**
```yaml
schedule:
  timezone: "Europe/London"
  active_window_start: "07:00"
  active_window_end: "21:00"
  briefing_window_end: "10:00"
  min_minutes_between_sessions: 90
```

**Tests:** 16 new unit tests in `test_orchestrator.py` (89 total)

**Key decisions:**
- No engagement model yet — cooldown + window rules are sufficient for now; actual adaptive timing (response latency, time-slot learning) can come later when there's 2+ weeks of usage data
- `zoneinfo` (stdlib Python 3.9+) used instead of `pytz` — no new dependency
- Goal-first framing is entirely in prompts — no code changes to goals.py, probe.py, or briefing.py; the data pipeline was already correct

---

## Phase 3 — Goal Layer

**Goal:** Tasks and habits are linked to longer-term goals. Claw can notice when a goal has gone quiet, anchor probe conversations in the goal's "why", and write back measurements automatically.

**Status:** ✅ Complete (revised) — 19 May 2026

**Definitions settled during design:**
- **Task** — discrete, completable action in work/home Todoist projects
- **Lifestyle** — recurring behaviour in the Claw project (exercise, eating well). Never completes. Tracked by ✓/✗ log.
- **Goal** — measurable desired outcome. Has a why, a target, a current measurement, an optional deadline. Lives in Todoist.

**Todoist structure:**
- "Goals" section in the Claw project (created by user)
- Each goal is a task whose description follows a structured template (Key: Value lines)
- Section is resolved dynamically by name — no hardcoded section ID required
- Task→Goal linking: shared Todoist labels (e.g. label `health` on both the exercise habit and the "Weight to 85kg" goal)

**Goal description template:**
```
Why: Feel confident, lighter, more energy day to day
Target: 85kg
Current: 108kg
By: 2026-12-01
Status: Diet consistent, exercise still patchy
```

**`claw/goals.py`** (rewritten):
- `GoalRecord` dataclass: `task_id`, `name`, `labels`, `why`, `target`, `current`, `by`, `status`
- `parse_goal_description(desc)` — parses Key: Value lines, case-insensitive, never raises
- `get_goals(todoist)` — fetches Goals section from Todoist and returns `list[GoalRecord]`
- `goal_for_task(task, goals)` — first goal with overlapping labels
- `build_goal_summary(tasks, goals, memory)` — per goal: current→target progress, last activity, `← QUIET` flag at 7+ days
- `goal_line_for_task(task, goals)` — multi-line context block for probe prompt: name, progress, deadline, why, status

**`claw/todoist_client.py`:**
- `get_goals()` — fetches Goals section by name; returns empty list if section doesn't exist
- `update_goal_current(task_id, value)` — reads description, replaces `Current:` line, writes back via existing `update_task_description()`
- `_update_description_field(desc, key, value)` — pure helper: replaces or appends a Key: Value line

**`claw/probe.py`:**
- `_detect_and_update_goal()` — new post-probe step: asks Claude (cheap model) if user mentioned a concrete measurement; if yes, writes it back to the goal's `Current:` field and sends Telegram confirmation

**Prompt changes:**
- `PROBE_SYSTEM` — use the gap, not the label: "you're at 108kg, aiming for 85"
- `TASK_SELECTION_SYSTEM` — deadline urgency rule: weight tasks from goals whose `By` is within 60 days
- `GOAL_UPDATE_DETECTION_SYSTEM` — new prompt: detects explicit measurements ("I weighed 107kg") from probe conversation; strict (no inference, no estimates)
- `BRIEFING_SYSTEM`, `TASK_SELECTION_SYSTEM` — QUIET goal framing and urgency rules

**Config:**
- `goals:` removed from `config.example.yaml` — replaced with documentation comment explaining the Todoist template
- No config changes required for existing deployments

**Tidy — 19 May 2026:**
- `get_lifestyle_habits()` + `get_goals()` merged into `get_claw_data() -> (habits, goal_tasks)` — single Todoist fetch per run instead of two
- `get_goals(todoist)` signature changed to `get_goals(goal_tasks)` — takes pre-fetched tasks, no longer needs a TodoistClient reference
- `MemoryStore.get_task_memories(ids)` added — bulk SQL lookup; `build_goal_summary()` now uses it instead of N individual calls
- `build_goal_summary()` regression fixed — goals with no linked tasks in the current pool are shown again ("no linked tasks in current pool") rather than silently omitted
- Inline `from claw.goals import goal_for_task` inside `_detect_and_update_goal()` moved to module-level import

---

## Phase 4 — M-code Pending Message Registry

**Goal:** When the user is unavailable for an extended period (commuting, etc.), unanswered probe messages pile up with no way to reply later without losing context. Introduce a lightweight shorthand: each outbound probe gets an M-code (`M1`, `M2`, …) the user can reply to by name — individually or in bulk — at any point.

**Status:** ✅ Complete — 20 May 2026

**What was built:**

**State layer (`memory.py`):**
- `pending_messages` SQLite table — one row per outbound probe: `code`, `text`, `type`, `sent_at`, `status` (`pending`|`answered`), `answered_at`
- `assign_message_code(text, type, cap) -> Optional[str]` — finds the lowest free M1–M9 slot, inserts the row, returns the code; returns `None` if all slots up to `cap` are taken
- `close_message_code(code) -> Optional[dict]` — marks a code `answered`, returns the row (or `None` if not found)
- `pending_count() -> int` — count of pending rows
- `get_pending_messages() -> list[dict]` — all pending rows ordered by code

**Code assignment in `probe.py`:**
- Cap check at the top of `_run_probe_inner` — if `pending_count() >= cap`, skip probe silently (briefings are never gated)
- Before sending each probe message, `assign_message_code()` is called; message is prefixed: `"M2: Did you do weights today?"`
- Race-guard inside `_probe_one_task` — cap may fill between the outer check and the send; `assign_message_code` returns `None` in that case and the task is skipped
- If the user responds during the live poll window, `close_message_code()` is called so the slot is freed immediately

**Reply handling in `listener.py`:**
- Module-level regex `_CODE_RE = re.compile(r'\b(M\d)\b', re.IGNORECASE)` pre-filters M-code messages
- `_parse_code_replies(text)` — single pass using match objects; supports multi-code replies in one message: `"M2 - yeah, M1 - No"` → `[("M2", "yeah"), ("M1", "No")]`
- Fast-path in `_handle_message` — M-code replies bypass Claude intent classification entirely (cheaper, faster)
- `_handle_code_replies` — closes each code, sends a combined ack: `"Got it — M1, M2 closed."`; separately flags any unknown codes

**One-message-per-cron-run:**
- Listener processes the first valid message then breaks; remaining updates are deferred to the next 2-min cron cycle
- `consumed_offset` tracked locally; written to SQLite once after the loop (not per-update)

**Top-up (implicit, no new code):**
- When a slot is freed, the next cron cycle sees `pending_count < cap` and sends a new probe naturally — "bang the drum" behaviour with no special orchestration

**Config addition:**
```yaml
schedule:
  max_pending_messages: 3   # briefings bypass this cap
```

**Tidy (post-build `/simplify` review):**
- `cap` computed once in `_run_probe_inner` and passed to `_probe_one_task` — eliminated a double config read
- Offset write moved out of the per-update loop — was writing to SQLite on every update, including filtered/invalid ones
- `_parse_code_replies` collapsed from two passes to one using match object positions directly

**Key decisions:**
- Briefings bypass the cap entirely — they never call `pending_count()`
- M-codes are closed immediately when the user replies during a live probe session (not just by the listener)
- Follow-up escalation (reuse same code after silence timeout) deferred to MoSCoW "Should" backlog
- Code reuse across unrelated topics deferred to MoSCoW "Could/Won't now" — revisit after escalation is live

---

## Maintenance — 20 May 2026

**Todoist retry logic:**
- `todoist_client.py` had no retry on transient server errors — a single 502 or 503 immediately fired a Telegram error alert
- Added `_request_with_retry(method, url, **kwargs)` — up to 2 retries with exponential backoff (1s, 2s) on 502/503 and connection errors
- All HTTP call sites (`_fetch_all`, `close_task`, `update_task_description`, `update_goal_current`) now go through it
- Added `import time`, `import logging`, module-level constants `_RETRYABLE_STATUS = {502, 503}`, `_MAX_RETRIES = 2`

**Integration test fixes:**
- Hardcoded model `claude-sonnet-4-20250514` updated to `claude-sonnet-4-6` (old ID returns 404)
- `TASK_SELECTION_USER_TEMPLATE` format call missing `goal_context` and `previous_topic` (added in Phase 3) — added both
- `strip_json_fences()` added to the task selection test assertion — Haiku sometimes wraps JSON in fences even in integration tests

---

## Phase 5 — Daemon Architecture

**Goal:** Eliminate the crontab-driven execution model. Collapse the two-process (orchestrator + listener) system into a single persistent daemon that runs a background polling thread and dispatches messages from a shared queue. Remove the lock file. Fix the Telegram offset conflict.

**Status:** ✅ Complete — 20 May 2026

**What was built:**

- `claw/main.py` — new daemon entrypoint. Background thread runs `telegram.get_updates()` continuously and feeds a `queue.Queue`. Main loop: every 30 minutes calls `orchestrator.run_orchestrator(reply_queue=incoming)`; otherwise drains the queue via `listener.handle_update()`.
- `listener.handle_update(update, ..., reply_queue)` — new public function. Extracts the message, validates the user, and dispatches. Called by the daemon. `run_listener()` retained for script/manual use.
- `probe.run_probe(..., reply_queue)` — accepts optional `reply_queue`; threaded through `_run_probe_inner` → `_probe_one_task` → `_run_conversation_loop` → `telegram.wait_for_reply(timeout, reply_queue)`.
- `telegram.wait_for_reply(timeout, reply_queue)` — when `reply_queue` is provided, reads from the shared queue instead of calling `getUpdates` directly. Direct-poll path retained for script mode.
- `orchestrator.run_orchestrator(..., reply_queue)` — passes `reply_queue` through to `run_probe()`.
- `PROBE_LOCK_FILE` removed from `probe.py` and `listener.py`; lock file creation/removal removed from `run_probe()`.
- `Dockerfile` — removed crontab copy, changed `CMD` to `["python", "-m", "claw.main"]`.
- `docker/crontab` — removed.
- `pyproject.toml` — added `claw`, `claw-orchestrator`, `claw-listener` entry points.

**Key decisions:**

- No APScheduler — a simple `time.time()` check in the main loop is sufficient for a 30-minute interval; avoids a new dependency (see ADR-008)
- `reply_queue` is optional in all function signatures — script-mode paths (`run_probe`, `run_orchestrator`, `run_listener`) continue to work without it
- Polling thread is a daemon thread — it exits automatically when the main process exits; no explicit shutdown needed
- Offset written to SQLite on each update — survives restarts correctly; polling thread reads the persisted offset on startup

**What was learned:**

- Threading `reply_queue` through four call levels (`run_probe` → `_run_probe_inner` → `_probe_one_task` → `_run_conversation_loop`) is verbose but explicit — every caller knows whether it's in daemon or script mode. A module-level global would be simpler but makes testing harder.
- The `queue.Empty` timeout loop in `wait_for_reply` (reading in 1-second chunks until deadline) keeps the probe responsive without busy-waiting.

---

## LiteLLM Integration — May 2026

**Goal:** Remove direct Anthropic API dependency from Claw. Route all AI calls through the LiteLLM proxy at `192.168.1.100:4000`. Centralise key management in LiteLLM — Claw's `.env` no longer holds an Anthropic key.

**Status:** ✅ Complete — 20 May 2026

**What changed:**

- `claw/claude_client.py` — replaced `anthropic` library with `openai`. `ClaudeClient.__init__` now takes `base_url` + `api_key`. `_call_with_retry` uses `openai.OpenAI.chat.completions.create()`; system prompt prepended as `{"role": "system", ...}` message. `from_env()` reads `LITELLM_API_KEY` and `LITELLM_BASE_URL` (falls back to `config["litellm"]["base_url"]`).
- `config/config.example.yaml` — added `litellm.base_url: "http://192.168.1.100:4000"`. Updated `claude.selection_model` from `claude-haiku-4-5-20251001` to `groq-compound-mini` (Groq via LiteLLM, ~$0.50/$1.00 per MTok).
- `claw/config.py` — added `("litellm", "base_url")` to required validation keys.
- `pyproject.toml` / `Dockerfile` — swapped `anthropic>=0.25.0` for `openai>=1.0.0`.
- `.env` — replaced `ANTHROPIC_API_KEY` with `LITELLM_API_KEY`.
- `tests/unit/test_core.py` — config fixtures updated to include `litellm.base_url`; new test for missing `litellm.base_url` raises ValueError (90 tests total).
- `tests/integration/test_integration.py` — credential check and config dicts updated to use `LITELLM_API_KEY` and LiteLLM model aliases.

**Model tier mapping (LiteLLM aliases):**
- `claude-sonnet-4.6` → `anthropic/claude-sonnet-4-6` — used for probe conversations and briefings
- `llama-3.3-70b` → Groq Llama 3.3 70B — used for task selection, session summaries, detection calls

**Public interface unchanged:** all callers (`probe.py`, `briefing.py`, `listener.py`) continue calling `claude.complete()` / `claude.complete_with_history()` with the same signatures.

---

## Next Steps

Ordered by value vs. effort. None of these are committed — just the clearest candidates.

### High value, low effort — all done
**1. ✅ Persistent inbound listener** — Phase 1.7
**2. ✅ Briefing includes habits** — Phase 1.8
**3. ✅ Snooze by reply** — Phase 1.7
**4. ✅ Constant Cleaning (session chaining)** — Phase 1.9
**5. ✅ Waiting For tracking** — Phase 1.9
**6. ✅ On-demand probe** — Phase 1.9

### ✅ Phase 2 — Adaptive Cadence + Goal-First Psychology — complete 19 May 2026

### ✅ Phase 3 — Goal layer — complete 19 May 2026

### ✅ Phase 4 — M-code pending message registry — complete 20 May 2026

### ✅ Phase 5 — Daemon Architecture — complete 20 May 2026

### Phase 6 — Memory Pruning (planned, separate session)

Implement a two-tier memory model before session volume causes token/cost pressure:
1. **Working memory** — last 3–5 exchanges per task (passed to probe prompt as-is)
2. **Synthesized long-term memory** — nightly Claude (Haiku) run that compresses raw session transcripts into "user trait" and "task pattern" notes (e.g. "Jake avoids admin tasks on Fridays")

The synthesized notes replace raw transcripts in `build_context_block()`. Old raw transcripts can be pruned after summarisation. This keeps prompt size bounded while actually improving Claw's contextual reasoning over time.

### Phase 7 — Obsidian Vault Integration (planned, separate session)

Write nightly synthesized memory summaries (from Phase 6) as markdown files into an Obsidian vault. Allows the PM Engine to read the "emotional state" of a project — not just task completion status. Depends on Phase 6 output existing first.

### MoSCoW backlog
- **Follow-up escalation (Should)** — if an M-code has been pending >N hours, the next probe references that topic with fresh urgency using the same code. No new state model needed — just check `sent_at` on pending rows.
- **Sentiment tracking (Could)** — score each session for emotional tone, build a rolling picture; tone calibration becomes more sophisticated
- **Webhook-based Telegram (Could)** — requires a public HTTPS endpoint (reverse proxy on Unraid); daemon polling is reliable enough for now (ADR-008)
- **Code reuse across unrelated topics (Won't now)** — revisit after follow-up escalation is live

---

## Lessons Learned

- **Haiku + JSON**: Always strip markdown code fences before parsing. Haiku wraps JSON in ` ```json ``` ` blocks despite being told not to. Fixed in `_select_task()`, `_write_habit_log()`, and `_detect_and_close()`.
- **UTC always**: Use `datetime.now(timezone.utc)` everywhere. Mixing naive and aware datetimes causes silent off-by-one errors in day calculations when the system timezone isn't UTC.
- **Prompt framing works**: The TASK_SELECTION prompt's habit instructions were understood immediately on the first live run — Haiku cited the exact reasoning we encoded ("early evening is high-value timing"). Prompt quality is the primary lever for behaviour.
- **Log in the source, not just the DB**: Writing the habit log back to Todoist description keeps the history where the habit lives. It's human-readable, editable, and Claude can see it without a separate DB query.
- **Post-conversation detection is cleaner than in-loop**: Detecting completion intent after the conversation ends (full transcript available) is simpler and more reliable than trying to detect it turn-by-turn during the loop.
- **Fuzzy subtask matching beats exact**: Claude paraphrases subtask names when reporting completion. Partial match fallback catches "got the bands" → "Find Resistance Bands" without needing the model to be precise.
- **M-codes and the cap**: The pending message cap is best enforced at the point of sending (inside `_probe_one_task`), not only at the outer gate. Two checks cost almost nothing and prevent a race where the cap fills between the orchestrator check and the actual send.
- **One message per cron, not one per update**: Processing one message per listener run and writing the offset once at the end (not per-update) avoids partial state where the offset advances past a message that wasn't actually handled.
- **Retry transient errors, don't alert on them**: 502/503 from Todoist are infrastructure blips. Retrying twice with backoff costs a few seconds and silences what would otherwise be noisy error alerts in Telegram. Save `send_error` for errors that persist after retries.
