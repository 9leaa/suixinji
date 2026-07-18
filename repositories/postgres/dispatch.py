"""Transactional Inbox, ordered root tasks, and Outbox command persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import InboxMessage, OutboxEvent, Space, Task
from memory.models import new_id
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime


@dataclass(frozen=True)
class DispatchResult:
    inbox_id: str
    task_id: str | None
    created: bool
    duplicate: bool


def _publish_task_request(session: Any, task: Task | str, task_type: str | None = None, *, attempt: int = 1) -> str:
    task_id = str(task.id if isinstance(task, Task) else task)
    resolved_type = str(task.task_type if isinstance(task, Task) else task_type or "")
    event_id = new_id("event")
    session.add(
        OutboxEvent(
            id=event_id,
            event_type="task.requested",
            aggregate_type="task",
            aggregate_id=task_id,
            payload_json={"task_id": task_id, "task_type": resolved_type, "attempt": max(1, int(attempt))},
        )
    )
    return event_id


def _enqueue_task_in_session(
    session: Any,
    *,
    task_type: str,
    tenant_id: str,
    space_id: str,
    source_message_id: str | None,
    idempotency_key: str,
    payload: dict[str, Any],
    priority: int = 0,
    max_attempts: int = 5,
    initial_status: str = "queued",
    publish: bool = True,
) -> tuple[str, bool]:
    if publish and initial_status != "queued":
        raise ValueError("only queued tasks may be published")
    task_id = new_id("task")
    created_id = session.execute(
        insert(Task)
        .values(
            id=task_id,
            task_type=task_type,
            tenant_id=tenant_id,
            space_id=space_id,
            source_message_id=source_message_id,
            idempotency_key=idempotency_key,
            status=initial_status,
            priority=priority,
            attempt_count=0,
            failure_count=0,
            defer_count=0,
            max_attempts=max_attempts,
            payload_json=payload,
        )
        .on_conflict_do_nothing(index_elements=[Task.idempotency_key])
        .returning(Task.id)
    ).scalar_one_or_none()
    if created_id is None:
        existing = session.execute(select(Task.id).where(Task.idempotency_key == idempotency_key)).scalar_one()
        return str(existing), False
    if publish:
        _publish_task_request(session, task_id, task_type, attempt=1)
    return task_id, True


def enqueue_task(
    *,
    task_type: str,
    space_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    tenant_id: str = DEFAULT_TENANT_ID,
    source_message_id: str | None = None,
    priority: int = 0,
    max_attempts: int = 5,
    initial_status: str = "queued",
    publish: bool = True,
) -> tuple[str, bool]:
    with session_scope() as session:
        ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        return _enqueue_task_in_session(
            session,
            task_type=task_type,
            tenant_id=tenant_id,
            space_id=space_id,
            source_message_id=source_message_id,
            idempotency_key=idempotency_key,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
            initial_status=initial_status,
            publish=publish,
        )


def receive_command(
    *,
    source: str,
    source_message_id: str,
    source_event_id: str | None,
    tenant_id: str,
    space_id: str,
    chat_id: str | None,
    chat_type: str | None,
    sender: dict[str, Any],
    text_value: str,
    received_at: str | datetime,
    task_type: str,
    task_payload: dict[str, Any],
    sensitivity: str = "normal",
    max_attempts: int = 5,
) -> DispatchResult:
    tenant_id = tenant_id or DEFAULT_TENANT_ID
    with session_scope() as session:
        ensure_tenant_space(session, space_id, tenant_id=tenant_id, source=source)
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:space_id))"), {"space_id": space_id})
        existing = session.execute(
            select(InboxMessage).where(
                InboxMessage.source == source,
                InboxMessage.source_message_id == source_message_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            task = session.execute(
                select(Task.id).where(Task.idempotency_key == f"{task_type}:{source}:{source_message_id}")
            ).scalar_one_or_none()
            return DispatchResult(existing.id, str(task) if task else None, False, True)

        has_pending = session.execute(
            select(InboxMessage.id)
            .where(InboxMessage.space_id == space_id, InboxMessage.status == "pending")
            .limit(1)
        ).scalar_one_or_none() is not None
        sequence_no = int(
            session.execute(
                select(func.coalesce(func.max(InboxMessage.sequence_no), 0) + 1).where(InboxMessage.space_id == space_id)
            ).scalar_one()
        )
        inbox_id = new_id("inbox")
        session.add(
            InboxMessage(
                id=inbox_id,
                source=source,
                source_message_id=source_message_id,
                source_event_id=source_event_id,
                tenant_id=tenant_id,
                space_id=space_id,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_json=sender,
                text=text_value,
                received_at=parse_datetime(received_at),
                status="pending",
                sensitivity=sensitivity,
                sequence_no=sequence_no,
            )
        )
        initial_status = "blocked" if has_pending else "queued"
        task_payload = {**task_payload, "inbox_id": inbox_id, "sequence_no": sequence_no}
        task_id, _ = _enqueue_task_in_session(
            session,
            task_type=task_type,
            tenant_id=tenant_id,
            space_id=space_id,
            source_message_id=source_message_id,
            idempotency_key=f"{task_type}:{source}:{source_message_id}",
            payload=task_payload,
            max_attempts=max_attempts,
            initial_status=initial_status,
            publish=not has_pending,
        )
        return DispatchResult(inbox_id, task_id, True, False)


def load_inbox_record(inbox_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        row = session.get(InboxMessage, inbox_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "source": row.source,
            "event_id": row.source_event_id,
            "message_id": row.source_message_id,
            "tenant_id": row.tenant_id,
            "space_id": row.space_id,
            "chat_id": row.chat_id,
            "chat_type": row.chat_type,
            "sender": dict(row.sender_json or {}),
            "ts": row.received_at.isoformat(),
            "text": row.text,
            "status": row.status,
            "sensitivity": row.sensitivity,
            "sequence_no": row.sequence_no,
        }


def is_next_inbox_message(inbox_id: str) -> bool:
    with session_scope() as session:
        row = session.get(InboxMessage, inbox_id)
        if row is None:
            return False
        earlier = session.execute(
            select(InboxMessage.id)
            .where(
                InboxMessage.space_id == row.space_id,
                InboxMessage.sequence_no < row.sequence_no,
                InboxMessage.status == "pending",
            )
            .limit(1)
        ).scalar_one_or_none()
        return earlier is None


def activate_task_in_session(session: Any, task_id: str) -> str | None:
    row = session.execute(select(Task).where(Task.id == task_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise ValueError(f"task not found: {task_id}")
    if row.status != "blocked":
        return None
    row.status = "queued"
    row.next_retry_at = None
    _publish_task_request(session, row, attempt=row.attempt_count + 1)
    return row.id


def _root_task_for_inbox(session: Any, inbox: InboxMessage) -> Task | None:
    return session.execute(
        select(Task)
        .where(
            Task.tenant_id == inbox.tenant_id,
            Task.space_id == inbox.space_id,
            Task.source_message_id == inbox.source_message_id,
            Task.payload_json["inbox_id"].as_string() == inbox.id,
        )
        .with_for_update()
    ).scalar_one_or_none()


def finalize_inbox_in_session(
    session: Any,
    inbox_id: str,
    *,
    success: bool,
    error: str | None = None,
) -> str | None:
    initial = session.get(InboxMessage, inbox_id)
    if initial is None:
        raise ValueError(f"inbox record not found: {inbox_id}")
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:space_id))"), {"space_id": initial.space_id})
    inbox = session.execute(
        select(InboxMessage).where(InboxMessage.id == inbox_id).with_for_update()
    ).scalar_one()
    if inbox.status in {"processed", "failed"}:
        return None

    inbox.status = "processed" if success else "failed"
    space = session.execute(select(Space).where(Space.id == inbox.space_id).with_for_update()).scalar_one()
    space.processed_sequence_no = max(int(space.processed_sequence_no or 0), int(inbox.sequence_no))
    if success:
        space.memory_watermark = max(int(space.memory_watermark or 0), int(inbox.sequence_no))
    else:
        space.memory_gap_sequence_no = int(inbox.sequence_no)
        metadata = dict(space.metadata_json or {})
        metadata["last_memory_gap"] = {
            "sequence_no": int(inbox.sequence_no),
            "inbox_id": inbox.id,
            "error_type": str(error or "unknown").split(":", 1)[0][:128],
        }
        space.metadata_json = metadata

    next_inbox = session.execute(
        select(InboxMessage)
        .where(InboxMessage.space_id == inbox.space_id, InboxMessage.status == "pending")
        .order_by(InboxMessage.sequence_no)
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()
    if next_inbox is None:
        return None
    next_task = _root_task_for_inbox(session, next_inbox)
    if next_task is None:
        raise RuntimeError(f"root task not found for inbox: {next_inbox.id}")
    return activate_task_in_session(session, next_task.id)


def mark_inbox_processed(inbox_id: str) -> str | None:
    with session_scope() as session:
        return finalize_inbox_in_session(session, inbox_id, success=True)


def mark_inbox_failed(inbox_id: str, error: str) -> str | None:
    with session_scope() as session:
        return finalize_inbox_in_session(session, inbox_id, success=False, error=error)


def get_space_progress(space_id: str) -> dict[str, int | None] | None:
    with session_scope() as session:
        row = session.get(Space, space_id)
        if row is None:
            return None
        return {
            "processed_sequence_no": int(row.processed_sequence_no or 0),
            "memory_watermark": int(row.memory_watermark or 0),
            "memory_gap_sequence_no": int(row.memory_gap_sequence_no) if row.memory_gap_sequence_no is not None else None,
        }
