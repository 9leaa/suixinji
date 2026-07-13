#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/data/suixinji.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "suixinji not running: no pid file"
  exit 1
fi

PID="$(cat "$PID_FILE")"

if kill -0 "$PID" 2>/dev/null; then
  echo "suixinji running, pid=$PID"
else
  echo "suixinji not running: stale pid=$PID"
  exit 1
fi
