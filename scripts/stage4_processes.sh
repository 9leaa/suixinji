#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
STATE_DIR="$ROOT/data/stage4"
PID_DIR="$STATE_DIR/pids"
LOG_DIR="$STATE_DIR/logs"
STATE_FILE="$STATE_DIR/current.env"

usage() {
  echo "usage: $0 start [run-id] | stop | status"
}

load_state() {
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "Stage 4 process state is missing: $STATE_FILE" >&2
    return 1
  fi
  # The state file contains generated identifiers and local endpoints only.
  source "$STATE_FILE"
}

start_role() {
  local role="$1"
  shift
  nohup "$@" >> "$LOG_DIR/$role.log" 2>&1 &
  echo $! > "$PID_DIR/$role.pid"
  echo "started $role pid=$(<"$PID_DIR/$role.pid")"
}

wait_receiver() {
  local endpoint="$1"
  for _ in $(seq 1 60); do
    if "$PYTHON" -c "import urllib.request; urllib.request.urlopen('$endpoint/health', timeout=1)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

start_all() {
  local run_id="${1:-$(date +%Y%m%d-%H%M%S)}"
  if [[ ! "$run_id" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "run-id may contain only letters, digits, dot, underscore, and dash" >&2
    exit 2
  fi
  if [[ -f "$STATE_FILE" ]]; then
    if "$0" status >/dev/null 2>&1; then
      echo "Stage 4 processes are already running" >&2
      exit 1
    fi
    rm -f "$STATE_FILE"
  fi
  rm -rf "$PID_DIR" "$LOG_DIR"
  mkdir -p "$PID_DIR" "$LOG_DIR"

  export STORAGE_BACKEND=postgres
  export COORDINATION_BACKEND=redis
  export TASK_QUEUE_BACKEND=redis_streams
  export SUIXINJI_ENV="stage4-$run_id"
  export SUIXINJI_FAKE_EXTERNALS=true
  export SUIXINJI_STAGE4_MODE=true
  export SUIXINJI_AGENT_HOOKS_ENABLED=true
  export SUIXINJI_STREAM_BLOCK_MS=200
  export SUIXINJI_STREAM_BATCH_SIZE=50
  export SUIXINJI_STREAM_CLAIM_IDLE_MS=15000
  export SUIXINJI_OUTBOX_BATCH_SIZE=100
  export SUIXINJI_OUTBOX_POLL_INTERVAL_SECONDS=0.05
  export SUIXINJI_WORKER_RETRY_BASE_SECONDS=0.1
  export SUIXINJI_DATABASE_POOL_SIZE=1
  export SUIXINJI_DATABASE_MAX_OVERFLOW=2
  export SUIXINJI_REDIS_MAX_CONNECTIONS=10
  export SUIXINJI_REDIS_SOCKET_TIMEOUT_SECONDS=5
  export SUIXINJI_REDIS_CONNECT_TIMEOUT_SECONDS=5

  cat > "$STATE_FILE" <<EOF
RUN_ID=$run_id
TENANT_ID=load-$run_id
REDIS_ENV=stage4-$run_id
RECEIVER_1=http://127.0.0.1:18101
RECEIVER_2=http://127.0.0.1:18102
STARTED_AT=$(date --iso-8601=seconds)
EOF

  start_role receiver-1 env SUIXINJI_PROCESS_ROLE=receiver "$PYTHON" -m uvicorn apps.api:app --host 127.0.0.1 --port 18101
  start_role receiver-2 env SUIXINJI_PROCESS_ROLE=receiver "$PYTHON" -m uvicorn apps.api:app --host 127.0.0.1 --port 18102
  start_role outbox-relay-1 env SUIXINJI_PROCESS_ROLE=outbox-relay "$PYTHON" -m apps.outbox_relay
  start_role outbox-relay-2 env SUIXINJI_PROCESS_ROLE=outbox-relay "$PYTHON" -m apps.outbox_relay
  for index in 1 2 3 4; do
    start_role "worker-ingest-$index" env SUIXINJI_PROCESS_ROLE=worker-ingest "$PYTHON" -m apps.worker ingest --worker-id "stage4-$run_id-ingest-$index"
  done
  for index in 1 2 3 4 5 6 7 8; do
    start_role "worker-memory-$index" env SUIXINJI_PROCESS_ROLE=worker-memory "$PYTHON" -m apps.worker memory --worker-id "stage4-$run_id-memory-$index"
  done
  for index in 1 2; do
    start_role "worker-query-$index" env SUIXINJI_PROCESS_ROLE=worker-query "$PYTHON" -m apps.worker query --worker-id "stage4-$run_id-query-$index"
    start_role "worker-summary-$index" env SUIXINJI_PROCESS_ROLE=worker-summary "$PYTHON" -m apps.worker summary --worker-id "stage4-$run_id-summary-$index"
    start_role "worker-enrichment-$index" env SUIXINJI_PROCESS_ROLE=worker-enrichment "$PYTHON" -m apps.worker enrichment --worker-id "stage4-$run_id-enrichment-$index"
    start_role "worker-delivery-$index" env SUIXINJI_PROCESS_ROLE=worker-delivery "$PYTHON" -m apps.worker delivery --worker-id "stage4-$run_id-delivery-$index"
    start_role "scheduler-$index" env SUIXINJI_PROCESS_ROLE=scheduler "$PYTHON" -m apps.scheduler
  done

  if ! wait_receiver "http://127.0.0.1:18101" || ! wait_receiver "http://127.0.0.1:18102"; then
    echo "Receiver startup failed; inspect $LOG_DIR" >&2
    stop_all
    exit 1
  fi
  echo "Stage 4 process matrix is ready: run_id=$run_id"
}

stop_all() {
  if [[ ! -d "$PID_DIR" ]]; then
    echo "Stage 4 processes are not running"
    return 0
  fi
  for pid_file in "$PID_DIR"/*.pid; do
    [[ -e "$pid_file" ]] || continue
    pid="$(<"$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for _ in $(seq 1 50); do
    running=0
    for pid_file in "$PID_DIR"/*.pid; do
      [[ -e "$pid_file" ]] || continue
      if kill -0 "$(<"$pid_file")" 2>/dev/null; then
        running=1
      fi
    done
    [[ "$running" -eq 0 ]] && break
    sleep 0.1
  done
  for pid_file in "$PID_DIR"/*.pid; do
    [[ -e "$pid_file" ]] || continue
    pid="$(<"$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  rm -rf "$PID_DIR"
  echo "Stage 4 processes stopped"
}

status_all() {
  load_state
  failed=0
  count=0
  for pid_file in "$PID_DIR"/*.pid; do
    [[ -e "$pid_file" ]] || continue
    role="$(basename "$pid_file" .pid)"
    pid="$(<"$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "$role: running pid=$pid"
      count=$((count + 1))
    else
      echo "$role: stopped"
      failed=1
    fi
  done
  echo "running_processes=$count run_id=$RUN_ID"
  [[ "$count" -eq 26 ]] || failed=1
  return "$failed"
}

cd "$ROOT"
case "${1:-}" in
  start) start_all "${2:-}" ;;
  stop) stop_all ;;
  status) status_all ;;
  *) usage; exit 2 ;;
esac
