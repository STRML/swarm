#!/usr/bin/env bash
# Ensure a local loopback-only Redis instance is running at SWARM_REDIS_URL.
set -euo pipefail

redis_url="${SWARM_REDIS_URL:-redis://127.0.0.1:6379/0}"
host_port="$(python3 -c 'import sys, urllib.parse as u; p = u.urlparse(sys.argv[1]); print(f"{p.hostname or \"127.0.0.1\"} {p.port or 6379}")' "$redis_url")"
read -r host port <<< "$host_port"

is_up() {
  redis-cli -h "$host" -p "$port" ping >/dev/null 2>&1
}

if is_up; then
  printf 'redis ready at %s:%s\n' "$host" "$port"
  exit 0
fi

if [[ "$port" == "6379" ]] && command -v brew >/dev/null 2>&1 && brew list --formula redis >/dev/null 2>&1; then
  brew services start redis >/dev/null
else
  redis-server --daemonize yes --bind 127.0.0.1 --port "$port" --save "" --appendonly no
fi

for _ in $(seq 1 25); do
  if is_up; then
    printf 'redis started at %s:%s\n' "$host" "$port"
    exit 0
  fi
  sleep 0.2
done

printf 'error: redis did not respond at %s:%s\n' "$host" "$port" >&2
exit 1
