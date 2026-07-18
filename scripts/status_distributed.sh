#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_DIR="$ROOT/data/pids"
failed=0

for role in outbox-relay worker-ingest worker-query worker-summary worker-memory worker-enrichment worker-delivery scheduler api receiver; do
  pid_file="$PID_DIR/$role.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(<"$pid_file")" 2>/dev/null; then
    echo "$role: running pid=$(<"$pid_file")"
  else
    echo "$role: stopped"
    failed=1
  fi
done

exit "$failed"
