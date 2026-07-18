"""Measured PostgreSQL and Redis metrics for one distributed load-test tenant."""

from __future__ import annotations

import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from redis.exceptions import RedisError, ResponseError
from sqlalchemy import func, select, text

from core.settings import DATABASE_GLOBAL_BUDGET
from infrastructure.database import session_scope
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import KEYS, RedisKeys
from infrastructure.schema import AgentRun, AgentStep, Delivery, InboxMessage, LlmUsage, OutboxEvent, Space, Task, TaskAttempt
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
            and task.idempotency_key
            == f"{task.tenant_id}:{task.task_type}:{source_by_message.get(task.source_message_id, '')}:{task.source_message_id}"
        ]
        task_ids = [task.id for task in tasks]
        attempts = list(session.execute(select(TaskAttempt).where(TaskAttempt.task_id.in_(task_ids))).scalars()) if task_ids else []
        outbox = list(session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id.in_(task_ids))).scalars()) if task_ids else []
        deliveries = list(session.execute(select(Delivery).where(Delivery.tenant_id == tenant_id)).scalars())
        runs = list(session.execute(select(AgentRun).where(AgentRun.tenant_id == tenant_id)).scalars())
        run_ids = [run.run_id for run in runs]
        usage = list(session.execute(select(LlmUsage).where(LlmUsage.run_id.in_(run_ids))).scalars()) if run_ids else []
        steps = list(session.execute(select(AgentStep).where(AgentStep.run_id.in_(run_ids))).scalars()) if run_ids else []
        spaces = list(session.execute(select(Space).where(Space.tenant_id == tenant_id)).scalars())
        connection_rows = list(
            session.execute(
                text(
                    "SELECT COALESCE(state, 'unknown') AS state, COUNT(*) AS count "
                    "FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name LIKE 'suixinji:%' GROUP BY COALESCE(state, 'unknown')"
                )
            )
        )

    root_status = _status_counts(root_tasks)
    all_status = _status_counts(tasks)
    queue_wait = [_duration_ms(task.created_at, task.started_at) for task in root_tasks]
    execution = [_duration_ms(task.started_at, task.completed_at) for task in root_tasks]
    latency = [_duration_ms(task.created_at, task.completed_at) for task in root_tasks]
    queue_wait_values = [value for value in queue_wait if value is not None]
    execution_values = [value for value in execution if value is not None]
    latency_values = [value for value in latency if value is not None]
    outbox_publish = [_duration_ms(event.created_at, event.published_at) for event in outbox]
    outbox_publish_values = [value for value in outbox_publish if value is not None]
    step_durations: dict[str, list[int]] = {}
    for step in steps:
        if step.duration_ms is None:
            continue
        step_durations.setdefault(str(step.step_type), []).append(int(step.duration_ms))
    latest_sequence_by_space: dict[str, int] = {}
    for row in inbox:
        latest_sequence_by_space[row.space_id] = max(
            latest_sequence_by_space.get(row.space_id, 0),
            int(row.sequence_no),
        )
    watermark_lags = [
        max(0, latest_sequence_by_space.get(space.id, 0) - int(space.memory_watermark or 0))
        for space in spaces
    ]
    note_watermark_lags = [
        max(0, latest_sequence_by_space.get(space.id, 0) - int(space.note_watermark or 0))
        for space in spaces
    ]
    connections_by_state = {str(state): int(count) for state, count in connection_rows}
    total_connections = sum(connections_by_state.values())
    return {
        "accepted": len(inbox),
        "inbox_pending": sum(1 for row in inbox if row.status == "pending"),
        "root_task_status": root_status,
        "all_task_status": all_status,
        "task_count": len(tasks),
        "retry_count": sum(1 for attempt in attempts if attempt.status in {"failed", "deferred"}),
        "failure_count": sum(int(task.failure_count or 0) for task in tasks),
        "defer_count": sum(int(task.defer_count or 0) for task in tasks),
        "outbox_unpublished": sum(1 for event in outbox if event.published_at is None and event.status != "dead"),
        "memory_gap_spaces": sum(1 for space in spaces if space.memory_gap_sequence_no is not None),
        "max_memory_watermark_lag": max(watermark_lags, default=0),
        "max_note_watermark_lag": max(note_watermark_lags, default=0),
        "outbox_dead": sum(1 for event in outbox if event.status == "dead"),
        "database_connections": {
            "by_state": connections_by_state,
            "total": total_connections,
            "global_budget": DATABASE_GLOBAL_BUDGET,
            "within_budget": total_connections <= DATABASE_GLOBAL_BUDGET,
        },
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
        "p50_outbox_publish_ms": percentile(outbox_publish_values, 0.50),
        "p95_outbox_publish_ms": percentile(outbox_publish_values, 0.95),
        "p99_outbox_publish_ms": percentile(outbox_publish_values, 0.99),
        "agent_step_latency_ms": {
            step_type: {
                "count": len(values),
                "p50": percentile(values, 0.50),
                "p95": percentile(values, 0.95),
                "p99": percentile(values, 0.99),
            }
            for step_type, values in sorted(step_durations.items())
        },
        "llm_requests": sum(int(row.request_count) for row in usage),
        "llm_tokens": sum(int(row.total_tokens) for row in usage),
        "estimated_cost": float(sum((Decimal(row.estimated_cost) for row in usage), Decimal("0"))),
    }


def _grouped_status_counts(session: Any, model: Any, tenant_id: str) -> dict[str, int]:
    rows = session.execute(
        select(model.status, func.count()).where(model.tenant_id == tenant_id).group_by(model.status)
    ).all()
    return {str(status): int(count) for status, count in rows}


def collect_wait_metrics(tenant_id: str) -> dict[str, Any]:
    """Collect queue progress with aggregate SQL suitable for frequent polling."""
    with session_scope() as session:
        inbox_status = _grouped_status_counts(session, InboxMessage, tenant_id)
        task_status = _grouped_status_counts(session, Task, tenant_id)
        task_count, failure_count, defer_count = session.execute(
            select(
                func.count(Task.id),
                func.coalesce(func.sum(Task.failure_count), 0),
                func.coalesce(func.sum(Task.defer_count), 0),
            ).where(Task.tenant_id == tenant_id)
        ).one()
        outbox_unpublished = session.execute(
            select(func.count(OutboxEvent.id))
            .select_from(OutboxEvent)
            .join(Task, Task.id == OutboxEvent.aggregate_id)
            .where(
                Task.tenant_id == tenant_id,
                OutboxEvent.aggregate_type == "task",
                OutboxEvent.published_at.is_(None),
                OutboxEvent.status != "dead",
            )
        ).scalar_one()
        latest_inbox = (
            select(
                InboxMessage.space_id.label("space_id"),
                func.max(InboxMessage.sequence_no).label("latest_sequence_no"),
            )
            .where(InboxMessage.tenant_id == tenant_id)
            .group_by(InboxMessage.space_id)
            .subquery()
        )
        space_count, memory_gap_spaces, max_watermark_lag, max_note_watermark_lag = session.execute(
            select(
                func.count(Space.id),
                func.count(Space.id).filter(Space.memory_gap_sequence_no.is_not(None)),
                func.coalesce(
                    func.max(
                        func.greatest(
                            func.coalesce(latest_inbox.c.latest_sequence_no, 0) - Space.memory_watermark,
                            0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.max(
                        func.greatest(
                            func.coalesce(latest_inbox.c.latest_sequence_no, 0) - Space.note_watermark,
                            0,
                        )
                    ),
                    0,
                ),
            )
            .select_from(Space)
            .outerjoin(latest_inbox, latest_inbox.c.space_id == Space.id)
            .where(Space.tenant_id == tenant_id)
        ).one()
        connection_count = int(
            session.execute(
                text(
                    "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name LIKE 'suixinji:%'"
                )
            ).scalar_one()
        )

    return {
        "accepted": sum(inbox_status.values()),
        "inbox_pending": int(inbox_status.get("pending") or 0),
        "inbox_status": inbox_status,
        "all_task_status": task_status,
        "task_count": int(task_count or 0),
        "failure_count": int(failure_count or 0),
        "defer_count": int(defer_count or 0),
        "outbox_unpublished": int(outbox_unpublished or 0),
        "space_count": int(space_count or 0),
        "memory_gap_spaces": int(memory_gap_spaces or 0),
        "max_memory_watermark_lag": int(max_watermark_lag or 0),
        "max_note_watermark_lag": int(max_note_watermark_lag or 0),
        "database_connections": connection_count,
        "database_connection_budget": DATABASE_GLOBAL_BUDGET,
    }


def _collect_stream_metrics_once(keys: RedisKeys) -> dict[str, Any]:
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


def collect_stream_metrics(keys: RedisKeys = KEYS, *, max_attempts: int = 3) -> dict[str, Any]:
    for attempt in range(max(1, max_attempts)):
        try:
            return _collect_stream_metrics_once(keys)
        except RedisError:
            if attempt + 1 >= max(1, max_attempts):
                raise
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("unreachable")


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


def reconcile_retry_submission(
    initial: dict[str, Any],
    retries: list[dict[str, Any]],
    *,
    database_accepted: int,
) -> dict[str, Any]:
    """Reconcile idempotent replays of the same request corpus."""
    if not retries:
        return dict(initial)
    submitted = int(initial.get("submitted") or 0)
    if any(int(item.get("submitted") or 0) != submitted for item in retries):
        raise ValueError("retry submission reports must contain the same request corpus")
    latest = retries[-1]
    final_rate_limited = int(latest.get("rate_limited") or 0)
    final_failed = int(latest.get("failed") or 0)
    intrinsic_duplicates = max(
        int(initial.get("duplicate") or 0),
        submitted - int(database_accepted) - final_rate_limited - final_failed,
    )
    confirmed_accepted = min(
        int(database_accepted),
        sum(int(item.get("accepted") or 0) for item in [initial, *retries]),
    )
    unresolved = max(0, submitted - int(database_accepted) - intrinsic_duplicates - final_rate_limited)
    reconciled = dict(initial)
    reconciled.update(
        {
            "accepted": confirmed_accepted,
            "duplicate": intrinsic_duplicates,
            "rate_limited": final_rate_limited,
            "failed": max(0, int(database_accepted) - confirmed_accepted) + unresolved,
            "retry_attempts": len(retries),
            "retry_duration_ms": sum(int(item.get("duration_ms") or 0) for item in retries),
        }
    )
    return reconciled


def build_report(
    database: dict[str, Any],
    streams: dict[str, Any],
    *,
    submission: dict[str, Any] | None = None,
    locks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    submission = submission or {}
    root_status = database.get("root_task_status") or {}
    blocked = int(root_status.get("blocked") or 0)
    queued = int(root_status.get("queued") or 0)
    running = int(root_status.get("running") or 0)
    retry = int(root_status.get("retry") or 0)
    completed = int(root_status.get("completed") or 0)
    cancelled = int(root_status.get("cancelled") or 0)
    dead_letter = int(root_status.get("dead_letter") or 0)
    duplicate = int(submission.get("duplicate") or 0)
    rate_limited = int(submission.get("rate_limited") or 0)
    submission_failed = int(submission.get("failed") or 0)
    accepted = int(database.get("accepted") or 0)
    client_confirmed_accepted = int(submission.get("accepted") if "accepted" in submission else accepted)
    accepted_after_client_timeout = min(submission_failed, max(0, accepted - client_confirmed_accepted))
    submission_failed_unaccepted = max(0, submission_failed - accepted_after_client_timeout)
    submitted = int(submission.get("submitted") or accepted + duplicate + rate_limited + submission_failed)
    pending = blocked + queued + running + retry
    failed = cancelled + dead_letter + submission_failed_unaccepted
    terminal_or_pending = completed + failed + pending + rate_limited + duplicate
    all_status = database.get("all_task_status") or {}
    all_pending = sum(int(all_status.get(name) or 0) for name in ("blocked", "queued", "running", "retry"))
    segmented_latency = {
        "receiver_acceptance": {
            "p50_ms": submission.get("p50_submission_latency_ms"),
            "p95_ms": submission.get("p95_submission_latency_ms"),
            "p99_ms": submission.get("p99_submission_latency_ms"),
        },
        "outbox_publish": {
            "p50_ms": database.get("p50_outbox_publish_ms"),
            "p95_ms": database.get("p95_outbox_publish_ms"),
            "p99_ms": database.get("p99_outbox_publish_ms"),
        },
        "queue_wait": {
            "p50_ms": database.get("p50_queue_wait_ms"),
            "p95_ms": database.get("p95_queue_wait_ms"),
            "p99_ms": database.get("p99_queue_wait_ms"),
        },
        "worker_execution": {
            "p50_ms": database.get("p50_execution_ms"),
            "p95_ms": database.get("p95_execution_ms"),
            "p99_ms": database.get("p99_execution_ms"),
        },
        "task_end_to_end": {
            "p50_ms": database.get("p50_latency_ms"),
            "p95_ms": database.get("p95_latency_ms"),
            "p99_ms": database.get("p99_latency_ms"),
        },
        "agent_steps": database.get("agent_step_latency_ms", {}),
    }
    return {
        "measurement_status": "measured",
        "submitted": submitted,
        "submission_retry_attempts": int(submission.get("retry_attempts") or 0),
        "submission_retry_duration_ms": int(submission.get("retry_duration_ms") or 0),
        "accepted": accepted,
        "client_confirmed_accepted": client_confirmed_accepted,
        "accepted_after_client_timeout": accepted_after_client_timeout,
        "submission_failed_unaccepted": submission_failed_unaccepted,
        "rate_limited": rate_limited,
        "duplicate": duplicate,
        "blocked": blocked,
        "queued": queued,
        "running": running,
        "completed": completed,
        "cancelled": cancelled,
        "failed": failed,
        "pending": pending,
        "dead_letter": dead_letter,
        "all_task_status": all_status,
        "all_task_pending": all_pending,
        "all_task_dead_letter": int(all_status.get("dead_letter") or 0),
        "retry_count": int(database.get("retry_count") or 0),
        "failure_count": int(database.get("failure_count") or 0),
        "defer_count": int(database.get("defer_count") or 0),
        "memory_gap_spaces": int(database.get("memory_gap_spaces") or 0),
        "max_memory_watermark_lag": int(database.get("max_memory_watermark_lag") or 0),
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
        "p50_outbox_publish_ms": database.get("p50_outbox_publish_ms"),
        "p95_outbox_publish_ms": database.get("p95_outbox_publish_ms"),
        "p99_outbox_publish_ms": database.get("p99_outbox_publish_ms"),
        "segmented_latency": segmented_latency,
        **(locks or {}),
        "llm_tokens": int(database.get("llm_tokens") or 0),
        "estimated_cost": float(database.get("estimated_cost") or 0),
        "failure_rate": round(failed / submitted, 6) if submitted else None,
        "conservation_delta": submitted - terminal_or_pending,
        "conservation_ok": submitted == terminal_or_pending,
        "database": database,
        "redis": streams,
    }
