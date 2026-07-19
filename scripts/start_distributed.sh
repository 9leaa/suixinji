#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
PID_DIR="$ROOT/data/pids"
LOG_DIR="$ROOT/data/logs"
failed=0

cd "$ROOT"
"$PYTHON" scripts/check_config.py
mkdir -p "$PID_DIR" "$LOG_DIR"

read -r API_HOST API_PORT < <("$PYTHON" - <<'PY'
import os
from dotenv import dotenv_values

values = dotenv_values(".env")
host = os.environ.get("SUIXINJI_API_HOST") or values.get("SUIXINJI_API_HOST") or "127.0.0.1"
port = os.environ.get("SUIXINJI_API_PORT") or values.get("SUIXINJI_API_PORT") or "8000"
print(str(host).strip() or "127.0.0.1", str(port).strip() or "8000")
PY
)

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

api_bind_available() {
  "$PYTHON" - "$API_HOST" "$API_PORT" <<'PY'
import socket
import sys

host = sys.argv[1]
try:
    port = int(sys.argv[2])
except ValueError:
    print(f"invalid API port: {sys.argv[2]}", file=sys.stderr)
    raise SystemExit(1)

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        print(f"api bind unavailable: {host}:{port} ({exc})", file=sys.stderr)
        raise SystemExit(1)
PY
}

start_role outbox-relay env SUIXINJI_PROCESS_ROLE=outbox-relay "$PYTHON" -m apps.outbox_relay
start_role worker-ingest env SUIXINJI_PROCESS_ROLE=worker-ingest "$PYTHON" -m apps.worker ingest
start_role worker-query env SUIXINJI_PROCESS_ROLE=worker-query "$PYTHON" -m apps.worker query
start_role worker-summary env SUIXINJI_PROCESS_ROLE=worker-summary "$PYTHON" -m apps.worker summary
start_role worker-memory env SUIXINJI_PROCESS_ROLE=worker-memory "$PYTHON" -m apps.worker memory
start_role worker-enrichment env SUIXINJI_PROCESS_ROLE=worker-enrichment "$PYTHON" -m apps.worker enrichment
start_role worker-delivery env SUIXINJI_PROCESS_ROLE=worker-delivery "$PYTHON" -m apps.worker delivery
start_role scheduler env SUIXINJI_PROCESS_ROLE=scheduler "$PYTHON" -m apps.scheduler
if [[ -f "$PID_DIR/api.pid" ]] && kill -0 "$(<"$PID_DIR/api.pid")" 2>/dev/null; then
  start_role api env SUIXINJI_PROCESS_ROLE=api "$PYTHON" -m uvicorn apps.api:app --host "$API_HOST" --port "$API_PORT"
elif api_bind_available; then
  start_role api env SUIXINJI_PROCESS_ROLE=api "$PYTHON" -m uvicorn apps.api:app --host "$API_HOST" --port "$API_PORT"
else
  echo "api not started; $API_HOST:$API_PORT is unavailable" >&2
  failed=1
fi
start_role receiver env SUIXINJI_PROCESS_ROLE=receiver "$PYTHON" -m bot.feishu_bot

exit "$failed"
