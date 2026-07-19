#!/usr/bin/env python
"""Generate or execute a Stage 4 multi-user workload. Execution is opt-in."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.load_testing import PROFILES, execute_load, generate_requests, summarize_plan


def default_endpoint() -> str:
    values = dotenv_values(ROOT / ".env")
    host = os.environ.get("SUIXINJI_API_HOST") or values.get("SUIXINJI_API_HOST") or "127.0.0.1"
    port = os.environ.get("SUIXINJI_API_PORT") or values.get("SUIXINJI_API_PORT") or "8000"
    return f"http://{str(host).strip() or '127.0.0.1'}:{str(port).strip() or '8000'}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(PROFILES), default="smoke")
    parser.add_argument("--users", type=int)
    parser.add_argument("--messages-per-user", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--endpoint", action="append", dest="endpoints")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=20260718)
    parser.add_argument("--run-id")
    parser.add_argument("--output")
    parser.add_argument("--execute", action="store_true", help="Actually submit requests. Without this flag only the workload plan is printed.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    profile = PROFILES[args.profile]
    users = args.users or profile.users
    messages_per_user = args.messages_per_user or profile.messages_per_user
    concurrency = args.concurrency or profile.concurrency
    requests = generate_requests(
        users=users,
        messages_per_user=messages_per_user,
        run_id=args.run_id,
        seed=args.seed,
    )
    if not args.execute:
        report = {
            **summarize_plan(requests),
            "mode": "dry_run",
            "concurrency": concurrency,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    else:
        endpoints = args.endpoints or [default_endpoint()]
        report = execute_load(
            requests,
            endpoint=endpoints,
            concurrency=concurrency,
            timeout_seconds=args.timeout_seconds,
        )
        report["mode"] = "executed"
        report["endpoints"] = endpoints
    output = Path(args.output) if args.output else ROOT / "data" / "load-tests" / f"{requests[0].run_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={output}")


if __name__ == "__main__":
    main()
