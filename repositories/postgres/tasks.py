"""PostgreSQL task state, attempts, retries, and idempotency."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from core.settings import TASK_LEASE_SECONDS
from infrastructure.schema import OutboxEvent, Task, TaskAttempt
from memory.models import new_id
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime
from repositories.postgres.dispatch import (
    activate_task_in_session,
    finalize_inbox_in_session,
    mark_inbox_memory_completed_in_session,
    mark_inbox_note_completed_in_session,
)


def create_task(task: dict[str, Any]) -> bool:
    space_id = str(task["space_id"])
    tenant_id = str(task.get("tenant_id") or DEFAULT_TENANT_ID)
    with session_scope() as session:
        space_id = ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        created = session.execute(
            insert(Task)
            .values(
                id=str(task["id"]),
                task_type=str(task["task_type"]),
                tenant_id=tenant_id,
                space_id=space_id,
                source_message_id=task.get("source_message_id"),
                idempotency_key=str(task["idempotency_key"]),
                status=str(task.get("status") or "queued"),
                priority=int(task.get("priority") or 0),
                attempt_count=int(task.get("attempt_count") or 0),
                failure_count=int(task.get("failure_count") or 0),
                defer_count=int(task.get("defer_count") or 0),
                max_attempts=int(task.get("max_attempts") or 3),
                payload_json=dict(task.get("payload") or task.get("payload_json") or {}),
                next_retry_at=parse_datetime(task["next_retry_at"]) if task.get("next_retry_at") else None,
            )
            .on_conflict_do_nothing(index_elements=[Task.idempotency_key])
            .returning(Task.id)
        ).scalar_one_or_none()
        return created is not None


def get_task(task_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        row = session.get(Task, task_id)
        if row is None:
            return None
        return {column.name: getattr(row, column.name) for column in Task.__table__.columns}


def update_task_status(task_id: str, status: str, **updates: Any) -> None:
    allowed = {
        "attempt_count",
        "failure_count",
        "defer_count",
        "next_retry_at",
        "started_at",
        "completed_at",
        "last_error",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    values["status"] = status
    with session_scope() as session:
        session.execute(update(Task).where(Task.id == task_id).values(**values))


def claim_task(task_id: str, worker_id: str, *, stale_after_seconds: int = TASK_LEASE_SECONDS) -> dict[str, Any] | None:
    now = datetime.now().astimezone()
    stale_before = now - timedelta(seconds=max(1, stale_after_seconds))
    with session_scope() as session:
        row = session.execute(select(Task).where(Task.id == task_id).with_for_update()).scalar_one_or_none()
        if row is None or row.status in {"blocked", "cancelled", "completed", "dead_letter"}:
            return None
        if row.status == "running":
            lease_is_live = row.lease_expires_at is not None and row.lease_expires_at > now
            legacy_claim_is_live = row.lease_expires_at is None and row.started_at is not None and row.started_at > stale_before
            if lease_is_live or legacy_claim_is_live:
                return None
            session.execute(
                update(TaskAttempt)
                .where(TaskAttempt.task_id == row.id, TaskAttempt.attempt_no == row.attempt_count, TaskAttempt.status == "running")
                .values(status="lease_expired", finished_at=now)
            )
        if row.next_retry_at is not None and row.next_retry_at > now:
            return None
        lease_seconds = max(1, int(stale_after_seconds))
        row.status = "running"
        row.started_at = now
        row.attempt_count += 1
        row.claimed_by = worker_id
        row.lease_token = uuid.uuid4().hex
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.claim_version += 1
        session.add(
            TaskAttempt(
                task_id=row.id,
                worker_id=worker_id,
                attempt_no=row.attempt_count,
                status="running",
                started_at=now,
            )
        )
        session.flush()
        return {column.name: getattr(row, column.name) for column in Task.__table__.columns}


def renew_task_lease(
    task_id: str,
    *,
    lease_token: str,
    claim_version: int,
    lease_seconds: int = TASK_LEASE_SECONDS,
) -> bool:
    now = datetime.now().astimezone()
    with session_scope() as session:
        renewed = session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == "running",
                Task.lease_token == lease_token,
                Task.claim_version == int(claim_version),
            )
            .values(lease_expires_at=now + timedelta(seconds=max(1, int(lease_seconds))))
            .returning(Task.id)
        ).scalar_one_or_none()
        return renewed is not None


def _owned_running_task(
    session: Any,
    task_id: str,
    *,
    lease_token: str,
    claim_version: int,
    now: datetime,
) -> Task | None:
    return session.execute(
        select(Task)
        .where(
            Task.id == task_id,
            Task.status == "running",
            Task.lease_token == lease_token,
            Task.claim_version == int(claim_version),
            Task.lease_expires_at > now,
        )
        .with_for_update()
    ).scalar_one_or_none()


def complete_task(
    task_id: str,
    *,
    lease_token: str,
    claim_version: int,
    release_inbox_id: str | None = None,
    activate_task_id: str | None = None,
    note_ready_inbox_id: str | None = None,
    memory_ready_inbox_id: str | None = None,
    ingest_complete_inbox_id: str | None = None,
) -> bool:
    outcomes = [release_inbox_id, activate_task_id, note_ready_inbox_id, memory_ready_inbox_id, ingest_complete_inbox_id]
    if sum(value is not None for value in outcomes) > 1:
        raise ValueError("a task may produce only one Inbox/dependency outcome")
    now = datetime.now().astimezone()
    with session_scope() as session:
        row = _owned_running_task(
            session,
            task_id,
            lease_token=lease_token,
            claim_version=claim_version,
            now=now,
        )
        if row is None:
            return False
        row.status = "completed"
        row.completed_at = now
        row.next_retry_at = None
        row.last_error = None
        row.claimed_by = None
        row.lease_token = None
        row.lease_expires_at = None
        session.execute(
            update(TaskAttempt)
            .where(TaskAttempt.task_id == task_id, TaskAttempt.attempt_no == row.attempt_count)
            .values(status="completed", finished_at=now)
        )
        if activate_task_id:
            activate_task_in_session(session, activate_task_id)
        elif note_ready_inbox_id:
            mark_inbox_note_completed_in_session(session, note_ready_inbox_id)
        elif memory_ready_inbox_id:
            mark_inbox_memory_completed_in_session(session, memory_ready_inbox_id)
            finalize_inbox_in_session(session, memory_ready_inbox_id, success=True)
        elif ingest_complete_inbox_id:
            mark_inbox_note_completed_in_session(session, ingest_complete_inbox_id)
            mark_inbox_memory_completed_in_session(session, ingest_complete_inbox_id)
            finalize_inbox_in_session(session, ingest_complete_inbox_id, success=True)
        elif release_inbox_id:
            finalize_inbox_in_session(session, release_inbox_id, success=True)
        return True


def _barrier_inbox_id(row: Task) -> str | None:
    payload = dict(row.payload_json or {})
    value = payload.get("barrier_inbox_id") or payload.get("inbox_id")
    return str(value) if value else None


def _cancel_blocked_dependents(session: Any, parent_task_id: str, error: str, now: datetime) -> None:
    rows = list(
        session.execute(
            select(Task)
            .where(
                Task.status == "blocked",
                Task.payload_json["parent_task_id"].as_string() == parent_task_id,
            )
            .with_for_update()
        ).scalars()
    )
    for dependent in rows:
        dependent.status = "cancelled"
        dependent.completed_at = now
        dependent.last_error = f"parent task failed: {error}"[:2000]


def fail_task(
    task_id: str,
    error: str,
    *,
    retry_delay_seconds: float,
    lease_token: str,
    claim_version: int,
) -> str:
    now = datetime.now().astimezone()
    with session_scope() as session:
        row = _owned_running_task(
            session,
            task_id,
            lease_token=lease_token,
            claim_version=claim_version,
            now=now,
        )
        if row is None:
            return "stale"
        row.failure_count += 1
        exhausted = row.failure_count >= row.max_attempts
        row.status = "dead_letter" if exhausted else "retry"
        row.last_error = error[:2000]
        row.next_retry_at = None if exhausted else now + timedelta(seconds=max(0.1, retry_delay_seconds))
        row.completed_at = now if exhausted else None
        row.claimed_by = None
        row.lease_token = None
        row.lease_expires_at = None
        session.execute(
            update(TaskAttempt)
            .where(TaskAttempt.task_id == task_id, TaskAttempt.attempt_no == row.attempt_count)
            .values(
                status="dead_letter" if exhausted else "failed",
                finished_at=now,
                error_type=error.split(":", 1)[0][:255],
                error_summary=error[:2000],
            )
        )
        if exhausted:
            payload = dict(row.payload_json or {})
            if payload.get("inbox_id"):
                _cancel_blocked_dependents(session, row.id, error, now)
            barrier_inbox_id = _barrier_inbox_id(row)
            if barrier_inbox_id:
                if row.task_type == "memory":
                    mark_inbox_memory_completed_in_session(
                        session,
                        barrier_inbox_id,
                        success=False,
                        error=error,
                    )
                finalize_inbox_in_session(session, barrier_inbox_id, success=False, error=error)
        return row.status


def defer_task(
    task_id: str,
    reason: str,
    *,
    retry_delay_seconds: float,
    lease_token: str,
    claim_version: int,
) -> bool:
    now = datetime.now().astimezone()
    with session_scope() as session:
        row = _owned_running_task(
            session,
            task_id,
            lease_token=lease_token,
            claim_version=claim_version,
            now=now,
        )
        if row is None:
            return False
        row.status = "retry"
        row.defer_count += 1
        row.last_error = reason[:2000]
        row.next_retry_at = now + timedelta(seconds=max(0.1, retry_delay_seconds))
        row.claimed_by = None
        row.lease_token = None
        row.lease_expires_at = None
        session.execute(
            update(TaskAttempt)
            .where(TaskAttempt.task_id == task_id, TaskAttempt.attempt_no == row.attempt_count)
            .values(status="deferred", finished_at=now, error_summary=reason[:2000])
        )
        return True


def enqueue_due_retries(*, limit: int = 50, task_ids: list[str] | None = None) -> int:
    now = datetime.now().astimezone()
    count = 0
    with session_scope() as session:
        statement = select(Task).where(Task.status == "retry", or_(Task.next_retry_at.is_(None), Task.next_retry_at <= now))
        if task_ids is not None:
            statement = statement.where(Task.id.in_(task_ids))
        rows = list(
            session.execute(
                statement.order_by(Task.next_retry_at, Task.created_at)
                .limit(max(1, int(limit)))
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        for row in rows:
            event_id = new_id("event")
            session.add(
                OutboxEvent(
                    id=event_id,
                    event_type="task.requested",
                    aggregate_type="task",
                    aggregate_id=row.id,
                    payload_json={"task_id": row.id, "task_type": row.task_type, "attempt": row.attempt_count + 1},
                )
            )
            row.status = "queued"
            row.next_retry_at = None
            row.claimed_by = None
            row.lease_token = None
            row.lease_expires_at = None
            count += 1
    return count
