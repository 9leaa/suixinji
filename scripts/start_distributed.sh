#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
PID_DIR="$ROOT/data/pids"
LOG_DIR="$ROOT/data/logs"

cd "$ROOT"
"$PYTHON" scripts/check_config.py
mkdir -p "$PID_DIR" "$LOG_DIR"

start_role() {
  local role="$1"
  shift
  local pid_file="$PID_DIR/$role.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(<"$pid_file")" 2>/dev/null; then
    echo "$role already running, pid=$(<"$pid_file")"
    return
  fi
  nohup "$@" >> "$LOG_DIR/$role.log" 2>&1 &
  echo $! > "$pid_file"
  echo "started $role, pid=$(<"$pid_file")"
}

start_role outbox-relay "$PYTHON" -m apps.outbox_relay
start_role worker-ingest "$PYTHON" -m apps.worker ingest
start_role worker-query "$PYTHON" -m apps.worker query
start_role worker-summary "$PYTHON" -m apps.worker summary
start_role worker-memory "$PYTHON" -m apps.worker memory
start_role worker-enrichment "$PYTHON" -m apps.worker enrichment
start_role worker-delivery "$PYTHON" -m apps.worker delivery
start_role scheduler "$PYTHON" -m apps.scheduler
start_role api "$PYTHON" -m uvicorn apps.api:app --host 0.0.0.0 --port 8000
start_role receiver "$PYTHON" -m bot.feishu_bot
