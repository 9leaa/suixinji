#!/usr/bin/env python
"""Stage 4 Docker chaos runner. It is a command preview unless --execute is set."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE4_FILE = ROOT / "docker-compose.stage4.yml"


@dataclass(frozen=True)
class ChaosStep:
    scenario: str
    command: list[str]
    wait_seconds: float = 0


def _stage4(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(STAGE4_FILE), "-p", "suixinji-stage4", *args]


def application_steps(pause_seconds: float) -> list[ChaosStep]:
    return [
        ChaosStep("worker_crash", _stage4("kill", "ingest-worker"), pause_seconds),
        ChaosStep("worker_recovery", _stage4("up", "-d", "--scale", "ingest-worker=4", "ingest-worker")),
        ChaosStep("outbox_relay_restart", _stage4("restart", "outbox-relay"), pause_seconds),
        ChaosStep("delivery_timeout", _stage4("pause", "delivery-worker"), pause_seconds),
        ChaosStep("delivery_recovery", _stage4("unpause", "delivery-worker")),
        ChaosStep("llm_timeout_backlog", _stage4("pause", "query-worker"), pause_seconds),
        ChaosStep("query_recovery", _stage4("unpause", "query-worker")),
        ChaosStep("scheduler_leader_loss", _stage4("kill", "scheduler"), pause_seconds),
        ChaosStep("scheduler_leader_recovery", _stage4("up", "-d", "--scale", "scheduler=2", "scheduler")),
    ]


def infrastructure_steps(infra_compose: Path, pause_seconds: float) -> list[ChaosStep]:
    base = ["docker", "compose", "-f", str(infra_compose)]
    return [
        ChaosStep("redis_restart", [*base, "restart", "redis"], pause_seconds),
        ChaosStep("postgres_restart", [*base, "restart", "postgres"], pause_seconds),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--pause-seconds", type=float, default=3.0)
    parser.add_argument("--include-infrastructure", action="store_true")
    parser.add_argument("--infra-compose", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    steps = application_steps(max(0, args.pause_seconds))
    if args.include_infrastructure:
        if args.infra_compose is None:
            raise SystemExit("--include-infrastructure requires --infra-compose; external Redis/PostgreSQL are never restarted implicitly")
        steps.extend(infrastructure_steps(args.infra_compose.resolve(), max(0, args.pause_seconds)))
    results = []
    for step in steps:
        item = {**asdict(step), "executed": args.execute, "returncode": None}
        print(f"[{step.scenario}] {' '.join(step.command)}")
        if args.execute:
            completed = subprocess.run(step.command, cwd=ROOT, check=False)
            item["returncode"] = completed.returncode
            if completed.returncode != 0:
                results.append(item)
                break
            if step.wait_seconds:
                time.sleep(step.wait_seconds)
        results.append(item)
    report = {"mode": "executed" if args.execute else "dry_run", "steps": results}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
