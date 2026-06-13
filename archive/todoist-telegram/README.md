# todoist-telegram (decommissioned)

This is the original Node.js Todoist digest/nag bot that Claw replaced. It is **no
longer running** — there is no container for it on the server, it is not under PM2, and
its `state.json` froze on 21 May 2026. Claw (the Python daemon) now owns the shared
Telegram bot token and all Todoist interaction.

It is kept here only for historical reference (the section-as-temporal-signal logic and
the `parseWaitingFor` rules originated in this bot and informed Claw's design — see
`docs/DECISIONS.md` ADR-006).

**Provenance:** archived from `/mnt/zpool/appdata/todoist-telegram/` on 13 June 2026.
Excluded from the archive: `node_modules/`, `.env` (secrets), `state.json` (operational
state containing personal task content), and `package-lock.json`. All secrets in the
original were loaded from environment variables — nothing sensitive is committed here.

Do not run this — it would conflict with Claw's Telegram polling on the shared bot token.
