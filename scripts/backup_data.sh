#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$ROOT/backups"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/suixinji-data-$TS.tar.gz"

mkdir -p "$BACKUP_DIR"
cd "$ROOT"

if [[ ! -d data ]]; then
  echo "data directory not found"
  exit 1
fi

tar -czf "$OUT" data

echo "backup written: $OUT"
