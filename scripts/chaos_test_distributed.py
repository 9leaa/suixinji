#!/usr/bin/env python
"""Process-based Stage 4 chaos runner. Commands are previews unless --execute is set."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "data" / "stage4"
PID_DIR = STATE_DIR / "pids"
LOG_DIR = STATE_DIR / "logs"
STATE_FILE = STATE_DIR / "current.env"

ROLE_COMMANDS = {
    "worker-ingest-1": [sys.executable, "-m", "apps.worker", "ingest", "--worker-id", "{redis_env}-ingest-chaos"],
    "outbox-relay-1": [sys.executable, "-m", "apps.outbox_relay"],
    "scheduler-1": [sys.executable, "-m", "apps.scheduler"],
}


def load_state() -> dict[str, str]:
    if not STATE_FILE.exists():
        raise SystemExit(f"Stage 4 state is missing: {STATE_FILE}")
    state = {}
    for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            state[key] = value
    return state


def process_env(state: dict[str, str]) -> dict[str, str]:
    return {
        **os.environ,
        "STORAGE_BACKEND": "postgres",
        "COORDINATION_BACKEND": "redis",
        "TASK_QUEUE_BACKEND": "redis_streams",
        "SUIXINJI_ENV": state["REDIS_ENV"],
        "SUIXINJI_FAKE_EXTERNALS": "true",
        "SUIXINJI_STAGE4_MODE": "true",
        "SUIXINJI_AGENT_HOOKS_ENABLED": "true",
        "SUIXINJI_STREAM_BLOCK_MS": "200",
        "SUIXINJI_STREAM_BATCH_SIZE": "50",
        "SUIXINJI_STREAM_CLAIM_IDLE_MS": "1000",
        "SUIXINJI_OUTBOX_BATCH_SIZE": "100",
        "SUIXINJI_OUTBOX_POLL_INTERVAL_SECONDS": "0.05",
        "SUIXINJI_WORKER_RETRY_BASE_SECONDS": "0.1",
        "SUIXINJI_DATABASE_POOL_SIZE": "1",
        "SUIXINJI_DATABASE_MAX_OVERFLOW": "2",
        "SUIXINJI_REDIS_MAX_CONNECTIONS": "10",
    }


def pid_for(role: str) -> int:
    return int((PID_DIR / f"{role}.pid").read_text(encoding="utf-8"))


def restart(role: str, state: dict[str, str]) -> int:
    command = [part.format(redis_env=state["REDIS_ENV"]) for part in ROLE_COMMANDS[role]]
    log_handle = (LOG_DIR / f"{role}.log").open("a", encoding="utf-8")
    process = subprocess.Popen(command, cwd=ROOT, env=process_env(state), stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True)
    (PID_DIR / f"{role}.pid").write_text(str(process.pid) + "\n", encoding="utf-8")
    return process.pid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--pause-seconds", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    state = load_state()
    steps: list[dict[str, Any]] = []
    for role in ("worker-ingest-1", "outbox-relay-1", "scheduler-1"):
        old_pid = pid_for(role)
        item: dict[str, Any] = {"scenario": f"restart_{role}", "old_pid": old_pid, "executed": args.execute}
        print(f"restart {role} pid={old_pid}")
        if args.execute:
            os.kill(old_pid, signal.SIGKILL)
            time.sleep(max(0.1, args.pause_seconds))
            item["new_pid"] = restart(role, state)
        steps.append(item)

    for role in ("worker-query-1", "worker-delivery-1"):
        pid = pid_for(role)
        item = {"scenario": f"pause_{role}", "pid": pid, "seconds": args.pause_seconds, "executed": args.execute}
        print(f"pause {role} pid={pid} seconds={args.pause_seconds}")
        if args.execute:
            os.kill(pid, signal.SIGSTOP)
            time.sleep(max(0.1, args.pause_seconds))
            os.kill(pid, signal.SIGCONT)
        steps.append(item)

    report = {"mode": "executed" if args.execute else "dry_run", "run_id": state["RUN_ID"], "steps": steps}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
