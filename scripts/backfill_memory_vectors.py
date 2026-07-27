#!/usr/bin/env python3
"""Schedule missing/current long-term Memory vectors through the outbox.

The script is safe to run repeatedly. It only enqueues work when
SUIXINJI_MEMORY_VECTOR_LIFECYCLE_ENABLED=true; actual embedding is performed
by the normal memory worker and remains retryable/idempotent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from repositories.postgres.memory import schedule_memory_vector_backfill


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", default="active", choices=("active", "inactive", "archived"))
    parser.add_argument("--limit", type=int, default=10000)
    args = parser.parse_args()
    scheduled = schedule_memory_vector_backfill(status=args.status, limit=args.limit)
    print(json.dumps({"status": "ok", "scheduled": scheduled, "memory_status": args.status}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
