"""PostgreSQL task state, attempts, retries, and idempotency."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import uuid

from sqlalchemy import or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from core.settings import TASK_LEASE_SECONDS
from infrastructure.schema import OutboxEvent, Task, TaskAttempt
from memory.models import new_id
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime
from repositories.postgres.dispatch import (
    activate_task_in_session,
    complete_inbox_stage_in_session,
    finalize_inbox_in_session,
    mark_inbox_memory_completed_in_session,
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
    lease_token = uuid.uuid4().hex
    lease_expires_at = now + timedelta(seconds=max(1, int(stale_after_seconds)))
    with session_scope() as session:
        row = session.execute(
            text(
                """
                WITH candidate AS (
                    SELECT task.*
                    FROM tasks AS task
                    WHERE task.id = :task_id
                      AND task.status NOT IN ('blocked', 'cancelled', 'completed', 'dead_letter')
                      AND (task.next_retry_at IS NULL OR task.next_retry_at <= :now)
                      AND (
                          task.status <> 'running'
                          OR (task.lease_expires_at IS NOT NULL AND task.lease_expires_at <= :now)
                          OR (
                              task.lease_expires_at IS NULL
                              AND (task.started_at IS NULL OR task.started_at <= :stale_before)
                          )
                      )
                    FOR UPDATE
                ), expired_attempt AS (
                    UPDATE task_attempts AS attempt
                    SET status = 'lease_expired', finished_at = :now
                    FROM candidate
                    WHERE candidate.status = 'running'
                      AND attempt.task_id = candidate.id
                      AND attempt.attempt_no = candidate.attempt_count
                      AND attempt.status = 'running'
                    RETURNING attempt.task_id
                ), claimed AS (
                    UPDATE tasks AS task
                    SET status = 'running',
                        started_at = :now,
                        attempt_count = candidate.attempt_count + 1,
                        claimed_by = :worker_id,
                        lease_token = :lease_token,
                        lease_expires_at = :lease_expires_at,
                        claim_version = candidate.claim_version + 1
                    FROM candidate
                    WHERE task.id = candidate.id
                    RETURNING task.*
                ), recorded_attempt AS (
                    INSERT INTO task_attempts(task_id, worker_id, attempt_no, status, started_at)
                    SELECT id, :worker_id, attempt_count, 'running', :now
                    FROM claimed
                    RETURNING task_id
                )
                SELECT claimed.*
                FROM claimed
                JOIN recorded_attempt ON recorded_attempt.task_id = claimed.id
                """
            ),
            {
                "task_id": task_id,
                "worker_id": worker_id,
                "lease_token": lease_token,
                "lease_expires_at": lease_expires_at,
                "now": now,
                "stale_before": stale_before,
            },
        ).mappings().one_or_none()
        return dict(row) if row is not None else None


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


def _complete_task_with_inbox_outcome(
    session: Any,
    task_id: str,
    *,
    lease_token: str,
    claim_version: int,
    inbox_id: str,
    note_completed: bool,
    memory_completed: bool,
    finalize: bool,
    now: datetime,
) -> bool:
    completed = session.execute(
        text(
            """
            WITH target_inbox AS MATERIALIZED (
                SELECT inbox.id, inbox.tenant_id, inbox.space_id, inbox.sequence_no
                FROM inbox_messages AS inbox
                WHERE inbox.id = :inbox_id
            ), locked_inbox AS MATERIALIZED (
                SELECT target_inbox.*,
                       pg_advisory_xact_lock(
                           hashtext(target_inbox.tenant_id || ':' || target_inbox.space_id)
                       ) AS lock_acquired
                FROM target_inbox
            ), completed AS (
                UPDATE tasks AS task
                SET status = 'completed',
                    completed_at = :now,
                    next_retry_at = NULL,
                    last_error = NULL,
                    claimed_by = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL
                FROM locked_inbox
                WHERE task.id = :task_id
                  AND task.tenant_id = locked_inbox.tenant_id
                  AND task.space_id = locked_inbox.space_id
                  AND task.status = 'running'
                  AND task.lease_token = :lease_token
                  AND task.claim_version = :claim_version
                  AND task.lease_expires_at > :now
                RETURNING task.id, task.attempt_count
            ), completed_attempt AS (
                UPDATE task_attempts AS attempt
                SET status = 'completed', finished_at = :now
                FROM completed
                WHERE attempt.task_id = completed.id
                  AND attempt.attempt_no = completed.attempt_count
                RETURNING attempt.task_id
            ), updated_inbox AS (
                UPDATE inbox_messages AS inbox
                SET note_status = CASE
                        WHEN :note_completed THEN 'completed' ELSE inbox.note_status
                    END,
                    note_completed_at = CASE
                        WHEN :note_completed THEN :now ELSE inbox.note_completed_at
                    END,
                    memory_status = CASE
                        WHEN :memory_completed THEN 'completed' ELSE inbox.memory_status
                    END,
                    memory_completed_at = CASE
                        WHEN :memory_completed THEN :now ELSE inbox.memory_completed_at
                    END,
                    status = CASE
                        WHEN :finalize AND inbox.status NOT IN ('processed', 'failed')
                            THEN 'processed'
                        ELSE inbox.status
                    END
                FROM completed, locked_inbox
                WHERE inbox.id = locked_inbox.id
                RETURNING inbox.id, inbox.space_id, inbox.sequence_no,
                          inbox.note_status, inbox.memory_status, inbox.status
            ), progress AS (
                SELECT space.id AS space_id,
                       updated_inbox.sequence_no,
                       GREATEST(
                           space.note_watermark,
                           COALESCE(
                               (
                                   SELECT MIN(candidate.sequence_no) - 1
                                   FROM inbox_messages AS candidate
                                   WHERE candidate.space_id = space.id
                                     AND candidate.sequence_no > space.note_watermark
                                     AND CASE
                                         WHEN candidate.id = updated_inbox.id
                                             THEN updated_inbox.note_status
                                         ELSE candidate.note_status
                                     END NOT IN ('completed', 'failed')
                               ),
                               (
                                   SELECT MAX(candidate.sequence_no)
                                   FROM inbox_messages AS candidate
                                   WHERE candidate.space_id = space.id
                                     AND candidate.sequence_no > space.note_watermark
                               ),
                               space.note_watermark
                           )
                       ) AS note_watermark,
                       GREATEST(
                           space.memory_watermark,
                           COALESCE(
                               (
                                   SELECT MIN(candidate.sequence_no) - 1
                                   FROM inbox_messages AS candidate
                                   WHERE candidate.space_id = space.id
                                     AND candidate.sequence_no > space.memory_watermark
                                     AND CASE
                                         WHEN candidate.id = updated_inbox.id
                                             THEN updated_inbox.memory_status
                                         ELSE candidate.memory_status
                                     END NOT IN ('completed', 'failed')
                               ),
                               (
                                   SELECT MAX(candidate.sequence_no)
                                   FROM inbox_messages AS candidate
                                   WHERE candidate.space_id = space.id
                                     AND candidate.sequence_no > space.memory_watermark
                               ),
                               space.memory_watermark
                           )
                       ) AS memory_watermark
                FROM updated_inbox
                JOIN spaces AS space ON space.id = updated_inbox.space_id
            ), updated_space AS (
                UPDATE spaces AS space
                SET note_watermark = progress.note_watermark,
                    memory_watermark = progress.memory_watermark,
                    processed_sequence_no = CASE
                        WHEN :finalize
                            THEN GREATEST(space.processed_sequence_no, progress.sequence_no)
                        ELSE space.processed_sequence_no
                    END
                FROM progress
                WHERE space.id = progress.space_id
                RETURNING space.id, space.note_watermark, space.memory_watermark
            ), activated AS (
                UPDATE tasks AS task
                SET status = 'queued',
                    next_retry_at = NULL,
                    claimed_by = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL
                FROM updated_space
                WHERE task.space_id = updated_space.id
                  AND task.status = 'blocked'
                  AND (
                      COALESCE(task.payload_json ->> 'consistency', 'weak') NOT IN ('note', 'memory')
                      OR (
                          task.payload_json ->> 'consistency' = 'note'
                          AND COALESCE(
                              NULLIF(task.payload_json ->> 'required_watermark', '')::bigint,
                              0
                          ) <= updated_space.note_watermark
                      )
                      OR (
                          task.payload_json ->> 'consistency' = 'memory'
                          AND COALESCE(
                              NULLIF(task.payload_json ->> 'required_watermark', '')::bigint,
                              0
                          ) <= updated_space.memory_watermark
                      )
                  )
                RETURNING task.id, task.task_type, task.attempt_count
            ), published AS (
                INSERT INTO outbox_events(
                    id, event_type, aggregate_type, aggregate_id, payload_json,
                    status, publish_attempt_count, max_attempts
                )
                SELECT 'event_' || MD5(activated.id || ':' || (activated.attempt_count + 1)::text),
                       'task.requested',
                       'task',
                       activated.id,
                       jsonb_build_object(
                           'task_id', activated.id,
                           'task_type', activated.task_type,
                           'attempt', activated.attempt_count + 1
                       ),
                       'pending',
                       0,
                       10
                FROM activated
                ON CONFLICT (id) DO NOTHING
                RETURNING aggregate_id
            )
            SELECT id FROM completed
            """
        ),
        {
            "task_id": task_id,
            "lease_token": lease_token,
            "claim_version": int(claim_version),
            "inbox_id": inbox_id,
            "note_completed": bool(note_completed),
            "memory_completed": bool(memory_completed),
            "finalize": bool(finalize),
            "now": now,
        },
    ).scalar_one_or_none()
    return completed is not None


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
        inbox_outcome = (
            (note_ready_inbox_id, True, False, False),
            (memory_ready_inbox_id, False, True, True),
            (ingest_complete_inbox_id, True, True, True),
            (release_inbox_id, False, False, True),
        )
        for inbox_id, note_completed, memory_completed, finalize in inbox_outcome:
            if inbox_id is not None:
                return _complete_task_with_inbox_outcome(
                    session,
                    task_id,
                    lease_token=lease_token,
                    claim_version=claim_version,
                    inbox_id=inbox_id,
                    note_completed=note_completed,
                    memory_completed=memory_completed,
                    finalize=finalize,
                    now=now,
                )
        if not any(outcomes):
            completed = session.execute(
                text(
                    """
                    WITH completed AS (
                        UPDATE tasks
                        SET status = 'completed',
                            completed_at = :now,
                            next_retry_at = NULL,
                            last_error = NULL,
                            claimed_by = NULL,
                            lease_token = NULL,
                            lease_expires_at = NULL
                        WHERE id = :task_id
                          AND status = 'running'
                          AND lease_token = :lease_token
                          AND claim_version = :claim_version
                          AND lease_expires_at > :now
                        RETURNING id, attempt_count
                    ), completed_attempt AS (
                        UPDATE task_attempts AS attempt
                        SET status = 'completed', finished_at = :now
                        FROM completed
                        WHERE attempt.task_id = completed.id
                          AND attempt.attempt_no = completed.attempt_count
                        RETURNING attempt.task_id
                    )
                    SELECT id FROM completed
                    """
                ),
                {
                    "task_id": task_id,
                    "lease_token": lease_token,
                    "claim_version": int(claim_version),
                    "now": now,
                },
            ).scalar_one_or_none()
            return completed is not None
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
            complete_inbox_stage_in_session(session, note_ready_inbox_id, note=True)
        elif memory_ready_inbox_id:
            complete_inbox_stage_in_session(session, memory_ready_inbox_id, memory=True, finalize=True)
        elif ingest_complete_inbox_id:
            complete_inbox_stage_in_session(
                session,
                ingest_complete_inbox_id,
                note=True,
                memory=True,
                finalize=True,
            )
        elif release_inbox_id:
            complete_inbox_stage_in_session(session, release_inbox_id, finalize=True)
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
