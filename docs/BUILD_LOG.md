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

## Phase 2 — Adaptive Timing

**Goal:** Claw learns when you're receptive and adjusts when it reaches out. Fixed cron is replaced (or supplemented) by a lightweight engagement model.

**Status:** 🔲 Not started — do not design in detail until Phase 1 is stable

**Rough scope:**
- Track response latency per session (how quickly you reply)
- Track response length (one-word replies = low engagement)
- Store engagement signal per time-of-day slot
- Let Claude factor engagement signal into whether to probe at a given time
- Introduce a "quiet mode" that Claw can detect and respect

**Open questions:**
- Does adaptive timing mean changing *when* the cron fires, or filtering at runtime?
- How many data points before the model is meaningful?
- What's the override mechanism if you want to force a briefing?

---

## Phase 3 — Goal Layer

**Goal:** Tasks are linked to longer-term goals. Claw can notice when you're making progress on a goal vs. drifting from it, and can contextualise task conversations accordingly.

**Status:** 🔲 Not started

**Rough scope:**
- Goal definition format (stored in config or vault)
- Task → Goal mapping (tag-based via Todoist labels, or explicit in config)
- Claude gains goal context when choosing what to probe
- Briefing can occasionally surface goal-level framing ("you've done nothing on Alpha in 10 days")

---

## Phase 4 — Sentiment Tracking

**Goal:** Claw builds a lightweight emotional model of you over time — not clinical, just aware. If you've been flat for a week, it might say something different than if you've been energised.

**Status:** 🔲 Not started

**Rough scope:**
- Session sentiment scored by Claude at end of each interaction (1-5 scale + notes)
- Sentiment history stored in memory
- System prompt gains rolling sentiment summary as context
- Tone calibration becomes more sophisticated

---

## Next Steps

Ordered by value vs. effort. None of these are committed — just the clearest candidates.

### High value, low effort
**1. ✅ Persistent inbound listener** — done in Phase 1.7. Two-minute cron, handles briefing queries and general messages.

**2. Briefing includes habits** — habits don't appear in the morning briefing at all. A brief mention of habit state ("Strength Training: 0 sessions logged this week") would make the briefing a fuller picture of the day.

**3. ✅ Snooze by reply** — done in Phase 1.7. Post-conversation detection writes `snoozed_until` to memory.

### Medium value, medium effort
**4. Adaptive timing (Phase 2)** — track how quickly and how much you reply per session, build a per-time-of-day engagement model. If you never reply on Monday evenings, stop probing then. Needs ~2 weeks of data before it's meaningful.

**5. Goal layer (Phase 3)** — link tasks to longer-term goals via Todoist labels. Claw notices when a goal has gone silent and surfaces it. Adds a layer of meaning above the task list.

### Lower priority
**6. Sentiment tracking (Phase 4)** — score each session for emotional tone, build a rolling picture. Useful once there's enough data (weeks). Claude's tone already adapts somewhat from context; this would formalise it.

**7. Webhook-based Telegram** — replace long-polling with webhooks for real-time response. Requires a public HTTPS endpoint (reverse proxy on Unraid).

---

## Lessons Learned

- **Haiku + JSON**: Always strip markdown code fences before parsing. Haiku wraps JSON in ` ```json ``` ` blocks despite being told not to. Fixed in `_select_task()`, `_write_habit_log()`, and `_detect_and_close()`.
- **UTC always**: Use `datetime.now(timezone.utc)` everywhere. Mixing naive and aware datetimes causes silent off-by-one errors in day calculations when the system timezone isn't UTC.
- **Prompt framing works**: The TASK_SELECTION prompt's habit instructions were understood immediately on the first live run — Haiku cited the exact reasoning we encoded ("early evening is high-value timing"). Prompt quality is the primary lever for behaviour.
- **Log in the source, not just the DB**: Writing the habit log back to Todoist description keeps the history where the habit lives. It's human-readable, editable, and Claude can see it without a separate DB query.
- **Post-conversation detection is cleaner than in-loop**: Detecting completion intent after the conversation ends (full transcript available) is simpler and more reliable than trying to detect it turn-by-turn during the loop.
- **Fuzzy subtask matching beats exact**: Claude paraphrases subtask names when reporting completion. Partial match fallback catches "got the bands" → "Find Resistance Bands" without needing the model to be precise.
