#!/usr/bin/env python
"""Wait until one Stage 4 tenant has no queued, running, retry, Outbox, or Stream work."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.distributed_metrics import collect_database_metrics, collect_stream_metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--expected-accepted", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--poll-seconds", type=float, default=1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    deadline = time.monotonic() + max(1, args.timeout_seconds)
    stable = 0
    while time.monotonic() < deadline:
        database = collect_database_metrics(args.tenant_id)
        streams = collect_stream_metrics()
        statuses = database["all_task_status"]
        task_pending = sum(int(statuses.get(name) or 0) for name in ("queued", "running", "retry"))
        settled = (
            database["accepted"] >= args.expected_accepted
            and database["inbox_pending"] == 0
            and database["outbox_unpublished"] == 0
            and task_pending == 0
            and streams["stream_lag"] == 0
            and streams["stream_pending"] == 0
        )
        stable = stable + 1 if settled else 0
        print(
            json.dumps(
                {
                    "accepted": database["accepted"],
                    "tasks": database["task_count"],
                    "task_status": statuses,
                    "inbox_pending": database["inbox_pending"],
                    "outbox_unpublished": database["outbox_unpublished"],
                    "stream_lag": streams["stream_lag"],
                    "stream_pending": streams["stream_pending"],
                    "stable_polls": stable,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if stable >= 3:
            return
        time.sleep(max(0.1, args.poll_seconds))
    raise SystemExit("Stage 4 run did not settle before timeout")


if __name__ == "__main__":
    main()
