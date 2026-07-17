"""PostgreSQL inbox repository replacing the local JSONL WAL."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import InboxMessage
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime


def _as_record(row: InboxMessage) -> dict[str, Any]:
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
    }


def append_message_once(record: Any) -> bool:
    values = asdict(record) if not isinstance(record, dict) else dict(record)
    source = str(values.get("source") or "feishu")
    space_id = str(values["space_id"])
    sender = dict(values.get("sender") or {})
    tenant_id = str(sender.get("tenant_key") or DEFAULT_TENANT_ID)
    with session_scope() as session:
        ensure_tenant_space(session, space_id, tenant_id=tenant_id, source=source)
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:space_id))"), {"space_id": space_id})
        next_sequence = int(
            session.execute(
                select(func.coalesce(func.max(InboxMessage.sequence_no), 0) + 1).where(InboxMessage.space_id == space_id)
            ).scalar_one()
        )
        result = session.execute(
            insert(InboxMessage)
            .values(
                id=str(values["id"]),
                source=source,
                source_message_id=str(values["message_id"]),
                source_event_id=values.get("event_id"),
                tenant_id=tenant_id,
                space_id=space_id,
                chat_id=values.get("chat_id"),
                chat_type=values.get("chat_type"),
                sender_json=sender,
                text=str(values.get("text") or ""),
                received_at=parse_datetime(values.get("ts")),
                status=str(values.get("status") or "pending"),
                sensitivity=str(values.get("sensitivity") or "normal"),
                sequence_no=next_sequence,
            )
            .on_conflict_do_nothing(constraint="uq_inbox_source_message")
            .returning(InboxMessage.id)
        ).scalar_one_or_none()
        return result is not None


def append_record(record: Any) -> None:
    append_message_once(record)


def list_wal_space_ids() -> list[str]:
    with session_scope() as session:
        return list(session.execute(select(InboxMessage.space_id).distinct().order_by(InboxMessage.space_id)).scalars())


def load_records(space_id: str) -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = session.execute(
            select(InboxMessage).where(InboxMessage.space_id == space_id).order_by(InboxMessage.sequence_no)
        ).scalars()
        return [_as_record(row) for row in rows]


def message_exists(space_id: str, message_id: str) -> bool:
    with session_scope() as session:
        return session.execute(
            select(InboxMessage.id).where(
                InboxMessage.space_id == space_id,
                InboxMessage.source_message_id == message_id,
            ).limit(1)
        ).scalar_one_or_none() is not None


def load_pending_records(space_id: str) -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = session.execute(
            select(InboxMessage)
            .where(InboxMessage.space_id == space_id, InboxMessage.status == "pending")
            .order_by(InboxMessage.sequence_no)
        ).scalars()
        return [_as_record(row) for row in rows]


def mark_processed(space_id: str, record_id: str) -> None:
    with session_scope() as session:
        session.execute(
            update(InboxMessage)
            .where(InboxMessage.space_id == space_id, InboxMessage.id == record_id)
            .values(status="processed")
        )


def mark_sensitive_blocked(space_id: str, record_id: str, category: str = "sensitive") -> None:
    with session_scope() as session:
        session.execute(
            update(InboxMessage)
            .where(InboxMessage.space_id == space_id, InboxMessage.id == record_id)
            .values(
                text="[敏感内容已拦截，原文未保存]",
                status="blocked_sensitive",
                sensitivity=category or "sensitive",
            )
        )
