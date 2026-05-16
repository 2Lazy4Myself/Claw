# Claw — Emotionally Intelligent Personal Assistant

## Purpose

Claw is a personal assistant that sits between your task list and your day. It is not a reminder system. It is not a productivity dashboard. It is a thoughtful, conversational agent that knows what you have to do, notices what isn't getting done, and asks good questions about why — without nagging, without overwhelming, and without treating you like a ticket queue.

It lives in Telegram. It reads from Todoist. It remembers what you've told it.

---

## Rationale

Most productivity tools fail for the same reason: they treat humans as rational actors who simply need to be reminded of their commitments. They escalate. They notify. They badge. They assume the blocker is memory.

The actual blockers are usually: ambiguity, energy, emotion, competing priorities, or the task having quietly become the wrong thing to do. No existing tool is built to ask about any of those.

Claw is built around a different model:

- **One thing at a time.** A daily briefing gives you the shape of the day, not a wall of demands. Then it might pick on one stuck thing — just one — and open a conversation.
- **Memory is the foundation of trust.** If it remembers what you said last week, it can ask a smarter question this week. That's what makes it feel like a person rather than a process.
- **Claude is the decision layer.** Logic like "pick the most overdue task" is not hardcoded. Claude reads context and decides. This means behaviour can be adjusted entirely through prompts, not code changes.
- **Tone adapts.** Warm when you need warmth, drier when you're clearly fine, quieter when you're clearly not in the mood.

---

## Architecture Overview

```
claw/
├── claw/
│   ├── briefing.py        # Morning summary: shape of the day
│   ├── probe.py           # Picks one stuck task, opens a conversation
│   ├── listener.py        # Handles inbound Telegram replies
│   ├── memory.py          # Per-task and per-session memory store
│   ├── todoist_client.py  # Todoist API wrapper
│   ├── telegram_client.py # Telegram bot wrapper
│   ├── claude_client.py   # Anthropic API wrapper
│   └── prompts.py         # All system/user prompts, versioned
├── tests/
│   ├── unit/              # Pure function tests, no I/O
│   └── integration/       # Live API tests, requires credentials
├── config/
│   ├── config.example.yaml
│   └── prompts.example.yaml
├── scripts/
│   ├── run_briefing.sh    # Called by cron
│   └── run_probe.sh       # Called by cron
├── docs/
│   ├── BUILD_LOG.md       # Phase-by-phase build history
│   ├── DECISIONS.md       # Architecture decision records
│   └── PROMPTS.md         # Prompt design notes
├── .env.example
├── requirements.txt
├── pyproject.toml
└── README.md
```

### Data Flow

```
Cron trigger
    → briefing.py / probe.py
        → todoist_client.py   (fetch tasks)
        → memory.py           (fetch task history + session context)
        → claude_client.py    (Claude decides what to say, using prompts.py)
        → telegram_client.py  (send message)
            ← user replies
        → listener.py         (receive reply)
        → memory.py           (log outcome, update task state)
```

---

## Design Principles

### No Monolithic Classes
Each module has a single, named responsibility. `todoist_client.py` fetches and normalises tasks. It does not decide what to do with them. `probe.py` orchestrates a probe conversation. It does not know how Todoist works. Cohesion is enforced by keeping files small and imports intentional.

### Configuration Lives Outside Code
All secrets, tunable values, and prompt text live in config files or environment variables — never hardcoded. `config/config.example.yaml` documents every available option. Copy it to `config/config.yaml` (gitignored) to run locally.

### Claude is the Logic Layer
Avoid encoding behaviour in Python that Claude can reason about better. Don't write `if task.days_overdue > 5: send_probe()`. Write a prompt that gives Claude the task list and memory context, and let it decide whether and what to probe. This keeps the codebase stable while behaviour evolves through prompt iteration.

### Prompts are Versioned and Documented
All prompts live in `claw/prompts.py` with named constants and inline comments explaining the intent. When a prompt changes, the reason goes in `docs/PROMPTS.md`. This makes it possible to understand why the agent behaves as it does months later.

### Tests are Not Optional
- **Unit tests** cover pure functions: task parsing, memory serialisation, prompt assembly. No mocks of I/O.
- **Integration tests** cover API round-trips: Todoist fetch, Claude response, Telegram send. These require real credentials and are tagged `@pytest.mark.integration` so they can be excluded from CI.
- Every new module ships with at least basic unit coverage. Regression tests are added whenever a bug is fixed.

### Fail Gracefully, Loudly
If Todoist is down, Claw sends nothing — it does not hallucinate tasks. If Claude returns an unexpected format, it logs and exits rather than sending garbage. Errors go to a designated Telegram error channel (configurable).

---

## Phases

See `docs/BUILD_LOG.md` for the full phase history. Summary:

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | MVP: briefing + probe + memory + Telegram + Todoist | 🔲 Not started |
| 2 | Adaptive timing — learn when you're responsive | 🔲 Not started |
| 3 | Goal layer — tasks linked to longer-term goals | 🔲 Not started |
| 4 | Sentiment tracking over time | 🔲 Not started |

---

## Getting Started

```bash
# Clone and set up
git clone <your-repo>
cd claw
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env                         # add your API keys
cp config/config.example.yaml config/config.yaml   # tune settings
cp config/prompts.example.yaml config/prompts.yaml # optional prompt overrides

# Run tests
pytest tests/unit/                           # no credentials needed
pytest tests/integration/ -m integration    # requires .env

# Run manually
python -m claw.briefing
python -m claw.probe
```

---

## Cron Setup (Unraid / Linux)

```cron
# Morning briefing at 8am
0 8 * * * /path/to/claw/scripts/run_briefing.sh

# Evening probe at 6pm
0 18 * * * /path/to/claw/scripts/run_probe.sh
```

---

## Contributing / Extending

When adding a new capability:

1. Create a new module with a single clear responsibility
2. Add unit tests in `tests/unit/`
3. Document the decision in `docs/DECISIONS.md` if it affects architecture
4. If it adds a new prompt, add it to `claw/prompts.py` with a comment
5. Update `docs/BUILD_LOG.md` with what was added and why
