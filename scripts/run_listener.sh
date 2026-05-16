#!/bin/bash
# run_listener.sh
# Called by cron every 2 minutes. Processes inbound Telegram messages.
# Exits immediately if a probe session is active (lock file present).
#
# Cron entry:
#   */2 * * * * /path/to/claw/scripts/run_listener.sh >> /path/to/claw/logs/listener.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

python -m claw.listener
