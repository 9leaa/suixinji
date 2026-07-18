#!/usr/bin/env python
"""Collect one Stage 4 run's PostgreSQL, Redis Streams, and latency metrics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.distributed_metrics import build_report, collect_database_metrics, collect_lock_metrics, collect_stream_metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--submission-report")
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    submission = {}
    if args.submission_report:
        submission = json.loads(Path(args.submission_report).read_text(encoding="utf-8"))
    database = collect_database_metrics(args.tenant_id)
    streams = collect_stream_metrics()
    locks = collect_lock_metrics(since=submission.get("started_at"))
    report = build_report(database, streams, submission=submission, locks=locks)
    report["tenant_id"] = args.tenant_id
    output = Path(args.output) if args.output else ROOT / "data" / "load-tests" / f"{args.tenant_id}-metrics.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={output}")


if __name__ == "__main__":
    main()
