#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/docker-compose.stage4.yml" -p suixinji-stage4)
EPHEMERAL_COMPOSE=(docker compose -f "$ROOT/docker-compose.stage4.yml" -f "$ROOT/docker-compose.stage4.ephemeral.yml" -p suixinji-stage4-ephemeral)
PYTHON="${PYTHON:-python3}"

preflight() {
  "$PYTHON" "$ROOT/scripts/check_stage4_network.py"
}

usage() {
  echo "usage: $0 up|down|status|logs|load [profile]|metrics <tenant-id> [submission-report]"
  echo "       $0 ephemeral-up|ephemeral-down|ephemeral-load [profile]|ephemeral-status"
}

ephemeral_env() {
  export STAGE4_DATABASE_URL="postgresql+psycopg://suixinji:suixinji@postgres:5432/suixinji"
  export STAGE4_REDIS_URL="redis://redis:6379/0"
}

case "${1:-}" in
  up)
    preflight
    "${COMPOSE[@]}" up -d --build \
      --scale receiver=2 \
      --scale outbox-relay=2 \
      --scale ingest-worker=4 \
      --scale query-worker=2 \
      --scale summary-worker=2 \
      --scale memory-worker=2 \
      --scale delivery-worker=2 \
      --scale scheduler=2
    ;;
  down)
    "${COMPOSE[@]}" down --remove-orphans
    ;;
  status)
    "${COMPOSE[@]}" ps
    ;;
  logs)
    "${COMPOSE[@]}" logs -f --tail=200
    ;;
  load)
    preflight
    profile="${2:-smoke}"
    "${COMPOSE[@]}" run --rm load-test \
      python scripts/load_test_multi_users.py \
      --profile "$profile" \
      --endpoint http://receiver:8000 \
      --execute
    ;;
  metrics)
    preflight
    tenant_id="${2:-}"
    submission_report="${3:-}"
    if [[ -z "$tenant_id" ]]; then
      usage
      exit 2
    fi
    args=(python scripts/collect_distributed_metrics.py --tenant-id "$tenant_id")
    if [[ -n "$submission_report" ]]; then
      args+=(--submission-report "$submission_report")
    fi
    "${COMPOSE[@]}" run --rm metrics "${args[@]}"
    ;;
  ephemeral-up)
    ephemeral_env
    "${EPHEMERAL_COMPOSE[@]}" up -d postgres redis
    "${EPHEMERAL_COMPOSE[@]}" run --rm migrate
    "${EPHEMERAL_COMPOSE[@]}" up -d --build \
      --scale receiver=2 \
      --scale outbox-relay=2 \
      --scale ingest-worker=4 \
      --scale query-worker=2 \
      --scale summary-worker=2 \
      --scale memory-worker=2 \
      --scale delivery-worker=2 \
      --scale scheduler=2 \
      receiver outbox-relay ingest-worker query-worker summary-worker memory-worker delivery-worker scheduler
    ;;
  ephemeral-down)
    ephemeral_env
    "${EPHEMERAL_COMPOSE[@]}" down -v --remove-orphans
    ;;
  ephemeral-load)
    ephemeral_env
    profile="${2:-smoke}"
    "${EPHEMERAL_COMPOSE[@]}" run --rm load-test \
      python scripts/load_test_multi_users.py \
      --profile "$profile" \
      --endpoint http://receiver:8000 \
      --execute
    ;;
  ephemeral-status)
    ephemeral_env
    "${EPHEMERAL_COMPOSE[@]}" ps
    ;;
  *)
    usage
    exit 2
    ;;
esac
