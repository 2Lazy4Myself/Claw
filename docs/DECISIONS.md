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

## ADR-005: No Shared Bot with openclaw / PM Engine

**Date:** Pre-Phase 1 (architectural prerequisite)  
**Status:** Accepted

**Context:**  
An existing Telegram bot is shared between openclaw and a PM engine. Adding Claw to the same bot would create three systems sharing one update stream, with no clear ownership of messages.

**Decision:** Claw gets its own dedicated bot token, created via BotFather.

**Rationale:**  
Shared bot tokens create fragile state — any system consuming `getUpdates` advances the offset, meaning other systems may miss messages. Each system having its own token means clean, independent update streams with no polling conflicts. This also makes it obvious to the user which system they're talking to.

**Consequences:**  
User manages three bot tokens. Acceptable — they are independent concerns. Bot names should be clearly distinct.
