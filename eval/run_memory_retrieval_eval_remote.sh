#!/usr/bin/env bash
set -euo pipefail

cd /home/zcj/suixinji
set -a
source /home/zcj/suixinji/.env
set +a

export DATABASE_URL="${DATABASE_URL/127.0.0.1:15432/127.0.0.1:25432}"
export REDIS_URL="redis://127.0.0.1:46379/0"
export SUIXINJI_DATABASE_POOL_SIZE=1
export SUIXINJI_DATABASE_MAX_OVERFLOW=0
export SUIXINJI_DATABASE_CONNECT_MAX_ATTEMPTS=20
export SUIXINJI_DATABASE_CONNECT_RETRY_BASE_SECONDS=1
export SUIXINJI_DATABASE_CONNECT_RETRY_MAX_SECONDS=3
export SUIXINJI_REDIS_MAX_CONNECTIONS=1
export SUIXINJI_REDIS_BLOCKING_MAX_CONNECTIONS=1
export SUIXINJI_EVAL_STACK_LOG="/tmp/${1}_stack.log"
export SUIXINJI_EVAL_VECTOR_BANK_ENABLED=true
export PYTHONUNBUFFERED=1

run_id="${1:?run id is required}"
limit="${2:-}"
output_dir="/home/zcj/suixinji/eval/results/${run_id}"
args=(
  /usr/local/anaconda3/envs/zcj_hello/bin/python
  /home/zcj/suixinji/eval/run_with_faulthandler.py
  /home/zcj/suixinji/eval/layer3/run_layer3_eval.py
  --data-dir
  /home/zcj/suixinji/eval/results/memory_retrieval_pooled120_dataset_20260831
  --output-dir
  "$output_dir"
  --run-id
  "$run_id"
  --top-k
  10
  --concurrency
  1
  --retrieval-only
)
if [[ -n "$limit" ]]; then
  args+=(--limit "$limit")
fi
exec "${args[@]}"
