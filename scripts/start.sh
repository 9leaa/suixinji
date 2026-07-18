#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
PID_FILE="$ROOT/data/suixinji.pid"
LOG_FILE="$ROOT/data/logs/runtime.log"

cd "$ROOT"

"$PYTHON" scripts/check_config.py

QUEUE_BACKEND="$("$PYTHON" -c 'from core.settings import TASK_QUEUE_BACKEND; print(TASK_QUEUE_BACKEND)')"
if [[ "$QUEUE_BACKEND" == "redis_streams" ]]; then
  exec bash "$ROOT/scripts/start_distributed.sh"
fi

mkdir -p "$ROOT/data/logs"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "suixinji already running, pid=$OLD_PID"
    exit 0
  fi
  echo "remove stale pid file: $PID_FILE"
  rm -f "$PID_FILE"
fi

nohup "$PYTHON" -m bot.feishu_bot >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

echo "suixinji started, pid=$(cat "$PID_FILE")"
echo "runtime log: $LOG_FILE"
