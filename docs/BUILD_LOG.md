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

## Lessons Learned

- **Haiku + JSON**: Always strip markdown code fences before parsing. Haiku wraps JSON in ` ```json ``` ` blocks despite being told not to. Fixed in `_select_task()` and `_write_habit_log()`.
- **UTC always**: Use `datetime.now(timezone.utc)` everywhere. Mixing naive and aware datetimes causes silent off-by-one errors in day calculations when the system timezone isn't UTC.
- **Prompt framing works**: The TASK_SELECTION prompt's habit instructions were understood immediately on the first live run — Haiku cited the exact reasoning we encoded ("early evening is high-value timing"). Prompt quality is the primary lever for behaviour.
- **Log in the source, not just the DB**: Writing the habit log back to Todoist description keeps the history where the habit lives. It's human-readable, editable, and Claude can see it in the task description without a separate DB query.
