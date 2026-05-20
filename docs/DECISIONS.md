# Architecture Decision Records

Each significant design decision is recorded here with context, the options considered, and the rationale. This prevents re-litigating settled decisions and helps future contributors (including future-you) understand why things are the way they are.

---

## ADR-001: Claude as the Logic Layer

**Date:** Phase 1  
**Status:** Accepted

**Context:**  
We need to decide what to probe, when to probe, and what tone to use. This logic could be encoded in Python (rules, scoring, branching) or delegated to Claude via prompt.

**Options considered:**
1. Python rules — e.g. `if task.overdue > 3 and not recently_discussed: probe()`
2. Claude decides — pass task list + memory context, let Claude choose

**Decision:** Claude decides.

**Rationale:**  
Rule-based logic encodes assumptions that will be wrong within weeks. "Most overdue" is not always the right task to probe — sometimes it's overdue because it's genuinely blocked, and probing again is annoying. Claude can reason about that. More importantly, behaviour can be adjusted by editing a prompt rather than shipping a code change. This keeps the codebase stable while the agent's personality and judgement evolve.

**Consequences:**  
Claude API costs are slightly higher per session. Accepted. Prompt quality becomes the primary lever for behaviour — so prompts must be versioned and documented carefully.

---

## ADR-002: SQLite for Memory

**Date:** Phase 1  
**Status:** Accepted

**Context:**  
Per-task history and session logs need to persist across restarts and be readable by multiple scripts (briefing, probe, listener).

**Options considered:**
1. JSON files — simple, human-readable, no dependencies
2. SQLite — queryable, atomic writes, handles concurrency better
3. Postgres — full relational DB, significant overhead for this scale

**Decision:** SQLite via the `sqlite3` stdlib module, wrapped in `memory.py`.

**Rationale:**  
JSON files have race condition risks when multiple scripts run close together (e.g. probe and listener both writing). SQLite gives atomic writes with no external dependency. The schema is simple enough that a full ORM would be overkill — raw SQL with parameterised queries is fine. The DB file path is configurable.

**Consequences:**  
Cannot be inspected as easily as JSON in a text editor, but can be queried with any SQLite browser tool. DB migrations will need to be handled manually if schema changes — acceptable at this scale.

---

## ADR-003: Polling over Webhook for Telegram (MVP)

**Date:** Phase 1  
**Status:** Accepted, revisit in Phase 2

**Context:**  
Telegram bots can receive messages via long-polling (bot asks Telegram repeatedly) or webhooks (Telegram pushes to a URL). Webhooks require a publicly accessible HTTPS endpoint.

**Decision:** Long-polling for MVP.

**Rationale:**  
Unraid runs behind a home network. Setting up a public HTTPS endpoint adds complexity (reverse proxy, SSL cert, port forwarding) that is out of scope for Phase 1. Polling is simpler, reliable enough for a single-user bot, and trivially replaced with webhooks later. The listener runs after a probe is sent and polls for a reply within a configurable timeout window.

**Consequences:**  
Responses to unsolicited messages (outside a probe session) will not be handled in MVP. Acceptable. Phase 2 may move to a persistent listener with webhook support.

---

## ADR-004: Prompts as Named Constants in Code, Overridable via Config

**Date:** Phase 1  
**Status:** Accepted

**Context:**  
Prompts need to be: (a) readable and version-controlled, (b) tweakable without a code deploy, (c) documented.

**Options considered:**
1. Hardcoded strings in each module — simple but not overridable
2. All prompts in a config YAML — overridable but not documented alongside code
3. Named constants in `prompts.py` with inline comments, optional override via `prompts.yaml`

**Decision:** Option 3.

**Rationale:**  
The prompt is part of the logic of the system. It belongs in the codebase, readable alongside the code that uses it. But for local tuning without a commit, a `prompts.yaml` override file allows quick iteration. `prompts.py` loads the YAML if present and falls back to the hardcoded defaults.

**Consequences:**  
`prompts.yaml` is gitignored. Anyone running their own instance can tune prompts without touching the codebase. Changes to default prompts in `prompts.py` are tracked in git and documented in `docs/PROMPTS.md`.

---

## ADR-005: Shared Bot Token with Parent Project

**Date:** Pre-Phase 1 (revised May 2026)  
**Status:** Accepted

**Context:**  
Claw is the child of the `todoist-telegram` project running on Unraid. Both systems share the same Telegram bot token (`8289598272:…`) and the same Todoist API token. OpenClaw also shares this bot token for its scheduled coach jobs.

**Decision:** Claw uses the shared bot token, not a dedicated one.

**Rationale:**  
Claw is designed to replace and extend `todoist-telegram` — it is not a separate concern. Using the same bot preserves continuity for the user. Polling conflicts are avoided by the fact that only one system actively polls at a time (Claw's probe listener runs within a bounded window, not continuously).

**Consequences:**  
Claw and `todoist-telegram` must not run simultaneously in listener mode. During Claw's probe session window, `todoist-telegram` is not polling. This is acceptable for the single-user, cron-driven architecture.

---

## ADR-007: M-code Pending Message Registry

**Date:** Phase 4 — May 2026
**Status:** Accepted

**Context:**
When the user is unavailable for an extended period, Claw either piles up unanswered messages or goes silent. There is no shorthand for replying to a specific message on return, and no mechanism to limit how many unresolved questions accumulate.

**Options considered:**
1. Queue all pending probes; replay them when the user is available — complex state, confusing UX
2. Just limit total messages (cap only, no reply tracking) — limits damage but provides no closure path
3. M-code registry — tag each probe with a code the user can reference when replying; cap the number of open codes

**Decision:** M-code registry (option 3).

**Rationale:**
The user wanted to be able to reply "M2 - yeah, M1 - No" in a single message hours later. That requires each probe to carry a stable short identifier. The registry tracks which codes are open, so Claw knows when a question has been answered. The cap prevents accumulation: if all slots are full, Claw goes quiet and tops up naturally when a slot is freed by the user responding.

Briefings bypass the cap entirely — they are a morning overview, not a question, and suppressing them during a backlog of unanswered probes would be the wrong trade-off.

**Key design choices:**
- Codes M1–M9 assigned in order; lowest free slot wins
- Cap is configurable (`schedule.max_pending_messages`, default 3)
- M-code replies detected by regex before Claude intent classification — faster and cheaper for a clear pattern
- Multi-code replies in one message supported: `"M2 - yeah, M1 - No"` parsed as two separate close actions
- Follow-up escalation (reusing a code for the same topic after silence) is deferred to the MoSCoW "Should" backlog

**Consequences:**
The listener now manages two concerns: processing inbound messages and closing pending M-codes. These are kept separate — the M-code fast-path runs before intent classification and returns early, so there's no interleaving.

---

## ADR-006: Sections as the Temporal Signal in Todoist

**Date:** May 2026  
**Status:** Accepted

**Context:**  
Jake's Todoist does not rely on native due dates as the primary way of expressing when a task should be done. Instead, sections within each project act as temporal buckets: Today / Next 2-3 Days / This Week / Next Week / This Month. Moving a task into a section IS the planning gesture.

**Decision:** `todoist_client.py` treats `section_name` as the primary time signal. `due_date` is secondary.

**Rationale:**  
This mirrors the established approach in the parent `todoist-telegram` project, which has been running successfully on Unraid. Forcing due-date-based logic onto a section-based workflow would produce incorrect results — tasks without a due_date are not undated, they're just dated by section. The briefing and probe logic must be aware of this: a task in "Next 2-3 Days" with no due_date is a near-term task, not an ambiguous one.

`due_date` is still used for two specific purposes:
1. Flagging overdue items (due_date < today) with visual emphasis
2. Including tasks from non-Today sections in the Today nag if their due_date has passed

**Consequences:**  
When Claude reasons about task urgency, prompts must include `section_name` as a key field. The `todoist_client.py` hardcodes the section IDs for Work and Home projects — these are stable and project-specific. If the Todoist structure ever changes, only `todoist_client.py` needs updating.

---

## ADR-008: Single-Process Daemon Replaces Crontab

**Date:** Phase 5  
**Status:** Accepted

**Context:**  
The Phase 1–4 architecture ran two separate cron processes: `*/30 orchestrator` and `*/2 listener`. Each process independently called `getUpdates` and tracked the Telegram offset in SQLite. A lock file (`/tmp/claw_probe.lock`) provided mutual exclusion during probe sessions. Two problems emerged:

1. **Offset conflict risk** — if the orchestrator triggered a probe that ran long (or if cron timing overlapped), both processes could call `getUpdates` simultaneously, one stealing the other's offset and silently dropping messages.
2. **Message latency** — the listener processed at most one message per 2-minute cron cycle.

**Options considered:**
1. Keep cron, add a more robust lock (systemd `.service` with `RemainAfterExit`, or a Redis-backed lock)
2. Collapse into a single daemon process with a background polling thread
3. Webhook via Cloudflare Tunnel (instant push, no polling at all)

**Decision:** Single-process daemon (option 2).

**Rationale:**  
A daemon with a background polling thread eliminates the offset conflict entirely — there is exactly one `getUpdates` consumer at all times. The polling thread feeds a `queue.Queue`; the probe reads replies from the same queue, so no lock file is needed. This approach requires no new infrastructure (unlike webhooks, which need a reverse proxy and external tunnel). APScheduler was evaluated but rejected — a simple time-based check in the main loop is sufficient for a 30-minute interval and avoids an additional dependency.

Webhook-based Telegram remains in the MoSCoW "Could" backlog. It would reduce idle compute and provide instant delivery, but requires a Cloudflare Tunnel or OPNsense port forwarding rule. The polling daemon is reliable enough for a single-user bot.

**Consequences:**  
- `claw/main.py` is the new entrypoint; Docker `CMD` changed to `python -m claw.main`
- `docker/crontab` removed
- `run_probe()`, `run_orchestrator()`, and `listener._handle_probe()` accept an optional `reply_queue` parameter; all script-mode paths continue to work without it
- Lock file (`PROBE_LOCK_FILE`) removed from `probe.py` and `listener.py`

---

## ADR-009: Route All AI Calls Through LiteLLM Proxy

**Date:** May 2026  
**Status:** Accepted

**Context:**  
Claw's `claude_client.py` originally called the Anthropic API directly using the `anthropic` Python library. This meant `ANTHROPIC_API_KEY` lived in Claw's `.env`. The user runs a LiteLLM proxy at `192.168.1.100:4000` that centralises all API credentials and supports model-level routing — the same proxy already used for Open WebUI and other tools.

**Decision:** Replace direct Anthropic calls with LiteLLM via the OpenAI-compatible `/v1/chat/completions` endpoint. All AI calls go through the proxy. `ANTHROPIC_API_KEY` is removed from Claw's environment.

**Two-tier model split (unchanged from Phase 1, only aliases updated):**
- **Powerful (probe, briefing):** `claude-sonnet-4.6` → LiteLLM routes to `anthropic/claude-sonnet-4-6`
- **Cheap (selection, summaries, detection):** `llama-3.3-70b` → LiteLLM routes to Groq Llama 3.3 70B

**Rationale:**  
Centralising key management in LiteLLM means Claw never holds an Anthropic key. Model swaps (e.g. cheap model → gemini-3.1-flash-lite) require only a `config.yaml` change, not a code change. The `openai` Python library's OpenAI-compatible interface is a clean drop-in for the `anthropic` library at this level of usage (text completions, no vision, no streaming).

**Consequences:**  
- `claude_client.py` uses `openai.OpenAI(base_url=..., api_key=...)` instead of `anthropic.Anthropic(...)`
- System prompt moves from Anthropic's separate `system=` param to `{"role": "system", "content": ...}` as the first message (standard OpenAI format)
- Response text: `response.choices[0].message.content` instead of `response.content[0].text`
- `LITELLM_API_KEY` added to `.env`; `ANTHROPIC_API_KEY` removed
- `config.yaml` gains `litellm.base_url`; `LITELLM_BASE_URL` env var overrides it if set
