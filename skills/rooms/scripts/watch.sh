#!/usr/bin/env bash
# Loop `swarm wait` and emit one JSON line per delivered message.
# Usage: watch.sh <agent-id> [room] [timeout-secs]
set -euo pipefail

AGENT="${1:?usage: watch.sh <agent-id> [room] [timeout-secs]}"
ROOM="${2:-${SWARM_ROOM:-main}}"
TIMEOUT="${3:-300}"

while true; do
  set +e
  OUT="$(swarm --room "$ROOM" --json wait --as "$AGENT" --timeout "$TIMEOUT" 2>&1)"
  RC=$?
  set -e
  if [[ $RC -eq 0 ]]; then
    printf '%s\n' "$OUT"
  elif [[ $RC -eq 10 ]]; then
    continue
  elif [[ $RC -eq 11 ]]; then
    printf '%s\n' "$OUT"
    exit 0
  else
    printf 'SWARM-ERROR (rc=%d): %s\n' "$RC" "$OUT" >&2
    sleep 5
  fi
done
