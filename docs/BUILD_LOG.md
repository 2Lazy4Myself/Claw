# Build Log

This document is the living record of what was built, when, why, and what was learned. It is updated at the end of every phase or significant change. It is not a changelog — it is a narrative.

---

## Phase 1 — MVP

**Goal:** A working, deployable system that does the two core things: sends a morning briefing and can open a probe conversation about one stuck task. Memory is real but simple. Tone is right. Nothing is hardcoded.

**Status:** 🔲 Not started

**Scope:**

- [ ] `todoist_client.py` — fetch today's tasks, parse into normalised Task objects
- [ ] `memory.py` — read/write per-task history and session log (SQLite)
- [ ] `claude_client.py` — thin wrapper around Anthropic API, handles retries and format errors
- [ ] `prompts.py` — BRIEFING_SYSTEM, PROBE_SYSTEM, PROBE_FOLLOWUP prompts (v1)
- [ ] `telegram_client.py` — send message, receive reply via polling or webhook
- [ ] `briefing.py` — orchestrates morning summary flow
- [ ] `probe.py` — orchestrates single-task probe conversation
- [ ] `listener.py` — handles inbound Telegram messages, routes to probe continuation
- [ ] Unit tests for: task parsing, memory serialisation, prompt assembly
- [ ] Integration tests for: Todoist fetch, Claude call, Telegram send
- [ ] `config/config.example.yaml` — documented config template
- [ ] `.env.example` — documented secrets template
- [ ] `scripts/run_briefing.sh` and `scripts/run_probe.sh`
- [ ] Cron-tested on Unraid

**Key decisions to make during Phase 1:**

- SQLite vs JSON for memory store (SQLite preferred — queryable, atomic writes)
- Webhook vs polling for Telegram (polling simpler for MVP, webhook for Phase 2)
- How to handle multi-turn probe conversations (stateful listener loop vs stateless with memory)

**Definition of done:**
Morning briefing fires, feels right. Probe picks one task, asks a real question, receives and logs the answer. Memory persists across restarts. No secrets in code.

---

**Phase 1 entries will be appended below as work progresses.**

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

*Appended as the project progresses.*
