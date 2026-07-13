"""Build measured runtime metrics from structured task logs."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


LOG_DIR = Path("data/logs")
OUTPUT_PATH = Path("docs/metrics/latest.json")


def load_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not LOG_DIR.exists():
        return events
    for path in sorted(LOG_DIR.glob("app-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, round((len(values) - 1) * ratio))
    return values[index]


def build_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    success_events = [
        event
        for event in events
        if event.get("action") == "runtime.task_success"
    ]
    failed_events = [
        event
        for event in events
        if event.get("action") == "runtime.task_failed"
    ]
    rejected_events = [
        event
        for event in events
        if event.get("action") == "runtime.task_rejected"
    ]

    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in success_events:
        task_type = str((event.get("extra") or {}).get("task_type") or "")
        by_type.setdefault(task_type, []).append(event)

    def durations(task_type: str, field: str) -> list[int]:
        values = []
        for event in by_type.get(task_type, []):
            value = (event.get("extra") or {}).get(field)
            if isinstance(value, int):
                values.append(value)
        return values

    total = len(success_events) + len(failed_events) + len(rejected_events)
    return {
        "measurement_status": "measured" if total else "not_measured",
        "p50_ingest_latency_ms": percentile(durations("ingest", "total_duration_ms"), 0.5),
        "p95_ingest_latency_ms": percentile(durations("ingest", "total_duration_ms"), 0.95),
        "p50_query_latency_ms": percentile(durations("query", "total_duration_ms"), 0.5),
        "p95_query_latency_ms": percentile(durations("query", "total_duration_ms"), 0.95),
        "p50_queue_wait_ms": percentile(
            [value for task_type in by_type for value in durations(task_type, "queue_wait_ms")],
            0.5,
        ),
        "task_success_rate": round(len(success_events) / total, 4) if total else None,
        "task_rejection_rate": round(len(rejected_events) / total, 4) if total else None,
        "task_count": total,
        "duration_mean_ms": round(statistics.mean(durations("ingest", "total_duration_ms")), 2)
        if durations("ingest", "total_duration_ms")
        else None,
    }


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if OUTPUT_PATH.exists():
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    existing.update(build_metrics(load_events()))
    OUTPUT_PATH.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
