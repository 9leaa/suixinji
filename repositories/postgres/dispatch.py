"""Transactional Inbox + Task + Outbox command persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import InboxMessage, OutboxEvent, Task
from memory.models import new_id
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime


@dataclass(frozen=True)
class DispatchResult:
    inbox_id: str
    task_id: str | None
    created: bool
    duplicate: bool


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
) -> tuple[str, bool]:
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
            status="queued",
            priority=priority,
            attempt_count=0,
            max_attempts=max_attempts,
            payload_json=payload,
        )
        .on_conflict_do_nothing(index_elements=[Task.idempotency_key])
        .returning(Task.id)
    ).scalar_one_or_none()
    if created_id is None:
        existing = session.execute(select(Task.id).where(Task.idempotency_key == idempotency_key)).scalar_one()
        return str(existing), False
    event_id = new_id("event")
    session.add(
        OutboxEvent(
            id=event_id,
            event_type="task.requested",
            aggregate_type="task",
            aggregate_id=task_id,
            payload_json={"task_id": task_id, "task_type": task_type, "attempt": 1},
        )
    )
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


def mark_inbox_processed(inbox_id: str) -> None:
    with session_scope() as session:
        session.execute(update(InboxMessage).where(InboxMessage.id == inbox_id).values(status="processed"))
