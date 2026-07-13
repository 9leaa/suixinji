#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="$ROOT/data/logs/runtime.log"

mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"

tail -n 100 -f "$LOG_FILE"
