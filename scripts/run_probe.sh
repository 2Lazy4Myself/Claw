#!/bin/bash
# run_probe.sh
# Called by cron each evening. Activates the virtualenv and runs the probe.
#
# Cron entry (6pm daily):
#   0 18 * * * /path/to/claw/scripts/run_probe.sh >> /path/to/claw/logs/probe.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

echo "[$(date -Iseconds)] Starting probe"
python -m claw.probe
echo "[$(date -Iseconds)] Probe complete"
