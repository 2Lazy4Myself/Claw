#!/bin/bash
# deploy.sh
# Git-based deploy for Claw. Run this ON THE UNRAID SERVER, inside the build
# clone at /mnt/zpool/appdata/claw/repo. See docs/DECISIONS.md ADR-010.
#
# Workflow:  edit -> commit -> push   (on the dev box)
#            ssh root@<server> 'cd /mnt/zpool/appdata/claw/repo && ./scripts/deploy.sh'
#
# Flow:
#   1. git pull (fast-forward only)
#   2. run unit tests in a throwaway container (no host Python needed)
#   3. docker build a CANDIDATE image (claw:candidate — never touches claw:latest)
#   4. smoke-check imports inside the candidate image
#   5. verify the candidate's claw/*.py checksums vs the running container
#   6. promote candidate -> claw:latest and swap the container, re-attaching the
#      existing data/config/.env/logs mounts
#
# Pass --verify-only to stop after step 5: builds + verifies the candidate with
# zero downtime — the running container and claw:latest are left untouched.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPDATA="/mnt/zpool/appdata/claw"   # holds the runtime mounts (data, logs, .env, config.yaml)
IMAGE="claw:latest"
CANDIDATE="claw:candidate"
CONTAINER="claw"

VERIFY_ONLY=0
[ "${1:-}" = "--verify-only" ] && VERIFY_ONLY=1

cd "$REPO_DIR"

echo "[deploy] 1/6 pulling latest..."
git pull --ff-only

echo "[deploy] 2/6 running unit tests..."
docker run --rm -v "$REPO_DIR":/app -w /app python:3.11-alpine \
  sh -c "pip install -q -r requirements.txt && python -m pytest -m 'not integration' -q"

echo "[deploy] 3/6 building candidate image ($CANDIDATE)..."
docker build -t "$CANDIDATE" .

echo "[deploy] 4/6 smoke-checking imports..."
docker run --rm "$CANDIDATE" \
  python -c "import claw.main, claw.orchestrator, claw.briefing, claw.probe, claw.listener, claw.nightly; print('imports ok')"

echo "[deploy] 5/6 verifying module checksums vs running container..."
if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  NEW="$(docker run --rm "$CANDIDATE" sh -c 'cd /app && sha256sum claw/*.py | sort')"
  CUR="$(docker exec "$CONTAINER" sh -c 'cd /app && sha256sum claw/*.py | sort')"
  if [ "$NEW" = "$CUR" ]; then
    echo "[deploy]   match — candidate is identical to the running container."
  else
    echo "[deploy]   candidate differs from the running container (expected for a real deploy):"
    diff <(echo "$CUR") <(echo "$NEW") || true
  fi
else
  echo "[deploy]   no running '$CONTAINER' container to compare against."
fi

if [ "$VERIFY_ONLY" -eq 1 ]; then
  echo "[deploy] --verify-only set: stopping before the swap. '$CANDIDATE' is built and verified;"
  echo "[deploy] the running container and '$IMAGE' are untouched."
  exit 0
fi

echo "[deploy] 6/6 promoting candidate and swapping container..."
docker tag "$CANDIDATE" "$IMAGE"
docker stop "$CONTAINER" 2>/dev/null || true
docker rm "$CONTAINER" 2>/dev/null || true
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -e TZ=Europe/London \
  -e PYTHONUNBUFFERED=1 \
  -v "$APPDATA/src/.env":/app/.env \
  -v "$APPDATA/src/config/config.yaml":/app/config/config.yaml \
  -v "$APPDATA/data":/app/data \
  -v "$APPDATA/logs":/logs \
  "$IMAGE"

echo "[deploy] done. Tailing startup logs:"
sleep 3
docker logs --tail 15 "$CONTAINER"
