#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
PID_DIR="$ROOT/data/pids"
LOG_DIR="$ROOT/data/logs"
failed=0

read -r API_HOST API_PORT < <("$PYTHON" - <<'PY'
import os
from dotenv import dotenv_values

values = dotenv_values(".env")
host = os.environ.get("SUIXINJI_API_HOST") or values.get("SUIXINJI_API_HOST") or "127.0.0.1"
port = os.environ.get("SUIXINJI_API_PORT") or values.get("SUIXINJI_API_PORT") or "8000"
print(str(host).strip() or "127.0.0.1", str(port).strip() or "8000")
PY
)

last_structured_event() {
  local role="$1"
  "$PYTHON" - "$LOG_DIR" "$role" <<'PY'
import json
import sys
from pathlib import Path

log_dir = Path(sys.argv[1])
role = sys.argv[2]
if not log_dir.exists():
    raise SystemExit(0)

for path in sorted(log_dir.glob("app-*.jsonl"), reverse=True):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        continue
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        extra = event.get("extra") or {}
        if extra.get("role") == role or extra.get("process_role") == role or extra.get("worker_id") == role:
            print(f"{event.get('ts')} {event.get('action')} {event.get('status')}")
            raise SystemExit(0)
PY
}

for role in outbox-relay worker-ingest worker-query worker-summary worker-memory worker-enrichment worker-delivery scheduler api receiver; do
  pid_file="$PID_DIR/$role.pid"
  log_file="$LOG_DIR/$role.log"
  log_mtime="missing"
  if [[ -f "$log_file" ]]; then
    log_mtime="$(date -r "$log_file" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo unknown)"
  fi
  if [[ -f "$pid_file" ]] && kill -0 "$(<"$pid_file")" 2>/dev/null; then
    pid="$(<"$pid_file")"
    started="$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//;s/ *$//')"
    uptime="$(ps -o etime= -p "$pid" 2>/dev/null | sed 's/^ *//;s/ *$//')"
    echo "$role: running pid=$pid start=${started:-unknown} uptime=${uptime:-unknown} log=$log_file log_mtime=$log_mtime"
    if [[ "$role" == "api" ]]; then
      if "$PYTHON" - "$API_HOST" "$API_PORT" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

host = sys.argv[1]
port = int(sys.argv[2])
urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1).read()
PY
      then
        echo "  health: http://$API_HOST:$API_PORT/health ok"
      else
        echo "  health: http://$API_HOST:$API_PORT/health failed"
        failed=1
      fi
    fi
  else
    echo "$role: stopped log=$log_file log_mtime=$log_mtime"
    failed=1
  fi
  event="$(last_structured_event "$role" || true)"
  if [[ -n "$event" ]]; then
    echo "  last_event: $event"
  fi
done

exit "$failed"
