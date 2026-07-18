"""Measured PostgreSQL and Redis metrics for one distributed load-test tenant."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from redis.exceptions import ResponseError
from sqlalchemy import select

from infrastructure.database import session_scope
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import KEYS, RedisKeys
from infrastructure.schema import AgentRun, Delivery, InboxMessage, LlmUsage, OutboxEvent, Task, TaskAttempt
from runtime.streams.client import GROUPS


def percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _status_counts(items: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.status)
        counts[status] = counts.get(status, 0) + 1
    return counts


def collect_database_metrics(tenant_id: str) -> dict[str, Any]:
    with session_scope() as session:
        inbox = list(session.execute(select(InboxMessage).where(InboxMessage.tenant_id == tenant_id)).scalars())
        tasks = list(session.execute(select(Task).where(Task.tenant_id == tenant_id)).scalars())
        source_by_message = {row.source_message_id: row.source for row in inbox}
        root_tasks = [
            task
            for task in tasks
            if task.source_message_id
            and task.idempotency_key == f"{task.task_type}:{source_by_message.get(task.source_message_id, '')}:{task.source_message_id}"
        ]
        task_ids = [task.id for task in tasks]
        attempts = list(session.execute(select(TaskAttempt).where(TaskAttempt.task_id.in_(task_ids))).scalars()) if task_ids else []
        outbox = list(session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id.in_(task_ids))).scalars()) if task_ids else []
        deliveries = list(session.execute(select(Delivery).where(Delivery.tenant_id == tenant_id)).scalars())
        runs = list(session.execute(select(AgentRun).where(AgentRun.tenant_id == tenant_id)).scalars())
        run_ids = [run.run_id for run in runs]
        usage = list(session.execute(select(LlmUsage).where(LlmUsage.run_id.in_(run_ids))).scalars()) if run_ids else []

    root_status = _status_counts(root_tasks)
    all_status = _status_counts(tasks)
    queue_wait = [_duration_ms(task.created_at, task.started_at) for task in root_tasks]
    execution = [_duration_ms(task.started_at, task.completed_at) for task in root_tasks]
    latency = [_duration_ms(task.created_at, task.completed_at) for task in root_tasks]
    queue_wait_values = [value for value in queue_wait if value is not None]
    execution_values = [value for value in execution if value is not None]
    latency_values = [value for value in latency if value is not None]
    return {
        "accepted": len(inbox),
        "inbox_pending": sum(1 for row in inbox if row.status == "pending"),
        "root_task_status": root_status,
        "all_task_status": all_status,
        "task_count": len(tasks),
        "retry_count": sum(1 for attempt in attempts if attempt.status in {"failed", "deferred"}),
        "outbox_unpublished": sum(1 for event in outbox if event.published_at is None),
        "delivery_status": _status_counts(deliveries),
        "p50_queue_wait_ms": percentile(queue_wait_values, 0.50),
        "p95_queue_wait_ms": percentile(queue_wait_values, 0.95),
        "p99_queue_wait_ms": percentile(queue_wait_values, 0.99),
        "p50_execution_ms": percentile(execution_values, 0.50),
        "p95_execution_ms": percentile(execution_values, 0.95),
        "p99_execution_ms": percentile(execution_values, 0.99),
        "p50_latency_ms": percentile(latency_values, 0.50),
        "p95_latency_ms": percentile(latency_values, 0.95),
        "p99_latency_ms": percentile(latency_values, 0.99),
        "llm_requests": sum(int(row.request_count) for row in usage),
        "llm_tokens": sum(int(row.total_tokens) for row in usage),
        "estimated_cost": float(sum((Decimal(row.estimated_cost) for row in usage), Decimal("0"))),
    }


def collect_stream_metrics(keys: RedisKeys = KEYS) -> dict[str, Any]:
    client = get_redis()
    lag = 0
    pending = 0
    streams: dict[str, dict[str, int]] = {}
    for task_type, group_name in GROUPS.items():
        stream = keys.stream(task_type)
        try:
            groups = client.xinfo_groups(stream)
        except ResponseError:
            groups = []
        group = next((item for item in groups if str(item.get("name")) == group_name), None)
        stream_lag = int((group or {}).get("lag") or 0)
        stream_pending = int((group or {}).get("pending") or 0)
        lag += stream_lag
        pending += stream_pending
        streams[task_type] = {"lag": stream_lag, "pending": stream_pending, "length": int(client.xlen(stream))}
    return {
        "stream_lag": lag,
        "stream_pending": pending,
        "dead_letter_stream": int(client.xlen(keys.dead_letter_stream())),
        "streams": streams,
    }


def collect_lock_metrics(*, since: str | None = None) -> dict[str, int | None]:
    since_dt = datetime.fromisoformat(since) if since else None
    values: list[int] = []
    log_dir = Path("data/logs")
    for path in sorted(log_dir.glob("app-*.jsonl")) if log_dir.exists() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("action") != "runtime.lock_acquired":
                continue
            if since_dt:
                event_time = datetime.fromisoformat(str(event.get("ts")))
                if event_time < since_dt:
                    continue
            value = (event.get("extra") or {}).get("lock_wait_ms")
            if isinstance(value, int):
                values.append(value)
    return {
        "p50_lock_wait_ms": percentile(values, 0.50),
        "p95_lock_wait_ms": percentile(values, 0.95),
        "p99_lock_wait_ms": percentile(values, 0.99),
    }


def build_report(
    database: dict[str, Any],
    streams: dict[str, Any],
    *,
    submission: dict[str, Any] | None = None,
    locks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    submission = submission or {}
    root_status = database.get("root_task_status") or {}
    queued = int(root_status.get("queued") or 0)
    running = int(root_status.get("running") or 0)
    retry = int(root_status.get("retry") or 0)
    completed = int(root_status.get("completed") or 0)
    dead_letter = int(root_status.get("dead_letter") or 0)
    duplicate = int(submission.get("duplicate") or 0)
    rate_limited = int(submission.get("rate_limited") or 0)
    submission_failed = int(submission.get("failed") or 0)
    accepted = int(database.get("accepted") or 0)
    client_confirmed_accepted = int(submission.get("accepted") if "accepted" in submission else accepted)
    accepted_after_client_timeout = min(submission_failed, max(0, accepted - client_confirmed_accepted))
    submission_failed_unaccepted = max(0, submission_failed - accepted_after_client_timeout)
    submitted = int(submission.get("submitted") or accepted + duplicate + rate_limited + submission_failed)
    pending = queued + running + retry
    failed = dead_letter + submission_failed_unaccepted
    terminal_or_pending = completed + failed + pending + rate_limited + duplicate
    all_status = database.get("all_task_status") or {}
    all_pending = sum(int(all_status.get(name) or 0) for name in ("queued", "running", "retry"))
    return {
        "measurement_status": "measured",
        "submitted": submitted,
        "accepted": accepted,
        "client_confirmed_accepted": client_confirmed_accepted,
        "accepted_after_client_timeout": accepted_after_client_timeout,
        "submission_failed_unaccepted": submission_failed_unaccepted,
        "rate_limited": rate_limited,
        "duplicate": duplicate,
        "queued": queued,
        "running": running,
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "dead_letter": dead_letter,
        "all_task_status": all_status,
        "all_task_pending": all_pending,
        "all_task_dead_letter": int(all_status.get("dead_letter") or 0),
        "retry_count": int(database.get("retry_count") or 0),
        "stream_lag": int(streams.get("stream_lag") or 0),
        "stream_pending": int(streams.get("stream_pending") or 0),
        "p50_latency_ms": database.get("p50_latency_ms"),
        "p95_latency_ms": database.get("p95_latency_ms"),
        "p99_latency_ms": database.get("p99_latency_ms"),
        "p50_queue_wait_ms": database.get("p50_queue_wait_ms"),
        "p95_queue_wait_ms": database.get("p95_queue_wait_ms"),
        "p99_queue_wait_ms": database.get("p99_queue_wait_ms"),
        "p50_execution_ms": database.get("p50_execution_ms"),
        "p95_execution_ms": database.get("p95_execution_ms"),
        "p99_execution_ms": database.get("p99_execution_ms"),
        **(locks or {}),
        "llm_tokens": int(database.get("llm_tokens") or 0),
        "estimated_cost": float(database.get("estimated_cost") or 0),
        "failure_rate": round(failed / submitted, 6) if submitted else None,
        "conservation_delta": submitted - terminal_or_pending,
        "conservation_ok": submitted == terminal_or_pending,
        "database": database,
        "redis": streams,
    }
