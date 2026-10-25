#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/environments/production/.env" ]]; then
  exec "$ROOT/.venv/bin/python" "$ROOT/scripts/local_services.py" stop production
fi
PID_DIR="$ROOT/data/pids"

if [[ ! -d "$PID_DIR" ]]; then
  echo "no distributed pid directory"
  exit 0
fi

for pid_file in "$PID_DIR"/*.pid; do
  [[ -e "$pid_file" ]] || continue
  role="$(basename "$pid_file" .pid)"
  pid="$(<"$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    echo "stopped $role, pid=$pid"
  fi
  rm -f "$pid_file"
done
