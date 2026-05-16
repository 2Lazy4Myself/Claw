#!/bin/bash
# run_briefing.sh
# Called by cron each morning. Activates the virtualenv and runs the briefing.
#
# Cron entry (8am daily):
#   0 8 * * * /path/to/claw/scripts/run_briefing.sh >> /path/to/claw/logs/briefing.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Activate virtualenv if present
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

echo "[$(date -Iseconds)] Starting briefing"
python -m claw.briefing
echo "[$(date -Iseconds)] Briefing complete"
