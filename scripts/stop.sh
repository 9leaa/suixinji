#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/data/suixinji.pid"

if [[ -d "$ROOT/data/pids" ]] && compgen -G "$ROOT/data/pids/*.pid" > /dev/null; then
  exec bash "$ROOT/scripts/stop_distributed.sh"
fi

if [[ ! -f "$PID_FILE" ]]; then
  echo "suixinji not running: no pid file"
  exit 0
fi

PID="$(cat "$PID_FILE")"

if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "stopped pid=$PID"
else
  echo "suixinji not running: stale pid=$PID"
fi

rm -f "$PID_FILE"
