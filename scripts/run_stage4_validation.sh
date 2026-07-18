#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
STATE_FILE="$ROOT/data/stage4/current.env"
PROFILE="${1:-basic}"

if [[ ! -f "$STATE_FILE" ]]; then
  echo "Start the process matrix first: bash scripts/stage4_processes.sh start" >&2
  exit 1
fi
source "$STATE_FILE"

export STORAGE_BACKEND=postgres
export COORDINATION_BACKEND=redis
export TASK_QUEUE_BACKEND=redis_streams
export SUIXINJI_ENV="$REDIS_ENV"
export SUIXINJI_FAKE_EXTERNALS=true
export SUIXINJI_STAGE4_MODE=true
export SUIXINJI_AGENT_HOOKS_ENABLED=true

REPORT_DIR="$ROOT/data/load-tests"
SUBMISSION_REPORT="$REPORT_DIR/$RUN_ID-submission.json"
CHAOS_REPORT="$REPORT_DIR/$RUN_ID-chaos.json"
METRICS_REPORT="$REPORT_DIR/$RUN_ID-metrics.json"
mkdir -p "$REPORT_DIR"

"$PYTHON" scripts/load_test_multi_users.py \
  --profile "$PROFILE" \
  --run-id "$RUN_ID" \
  --endpoint "$RECEIVER_1" \
  --endpoint "$RECEIVER_2" \
  --execute \
  --output "$SUBMISSION_REPORT"

"$PYTHON" scripts/chaos_test_distributed.py \
  --execute \
  --pause-seconds 1 \
  --output "$CHAOS_REPORT"

accepted="$($PYTHON -c "import json; print(json.load(open('$SUBMISSION_REPORT', encoding='utf-8'))['accepted'])")"
"$PYTHON" scripts/wait_distributed_run.py \
  --tenant-id "$TENANT_ID" \
  --expected-accepted "$accepted" \
  --timeout-seconds 900

"$PYTHON" scripts/collect_distributed_metrics.py \
  --tenant-id "$TENANT_ID" \
  --submission-report "$SUBMISSION_REPORT" \
  --output "$METRICS_REPORT"

echo "submission_report=$SUBMISSION_REPORT"
echo "chaos_report=$CHAOS_REPORT"
echo "metrics_report=$METRICS_REPORT"
