"""Lease-fenced PostgreSQL Outbox relay repository."""

from __future__ import annotations

import socket
import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import or_, select, update

from core.settings import OUTBOX_LEASE_SECONDS, OUTBOX_MAX_ATTEMPTS
from infrastructure.database import session_scope
from infrastructure.schema import OutboxEvent


class EventPublisher(Protocol):
    def publish_task(self, event_id: str, payload: dict[str, Any]) -> str: ...


def claim_outbox_batch(
    *,
    worker_id: str,
    limit: int = 50,
    event_ids: list[str] | None = None,
    lease_seconds: int = OUTBOX_LEASE_SECONDS,
) -> list[dict[str, Any]]:
    now = datetime.now().astimezone()
    with session_scope() as session:
        statement = select(OutboxEvent).where(
            OutboxEvent.published_at.is_(None),
            or_(
                (
                    OutboxEvent.status.in_(("pending", "retry"))
                    & or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now)
                ),
                (OutboxEvent.status == "publishing") & (OutboxEvent.lease_expires_at <= now),
            ),
        )
        if event_ids is not None:
            statement = statement.where(OutboxEvent.id.in_(event_ids))
        rows = list(
            session.execute(
                statement.order_by(OutboxEvent.created_at)
                .limit(max(1, int(limit)))
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        claimed: list[dict[str, Any]] = []
        for row in rows:
            token = uuid.uuid4().hex
            row.status = "publishing"
            row.claimed_by = worker_id
            row.lease_token = token
            row.lease_expires_at = now + timedelta(seconds=max(1, int(lease_seconds)))
            row.last_attempt_at = now
            row.publish_attempt_count += 1
            if not row.max_attempts:
                row.max_attempts = OUTBOX_MAX_ATTEMPTS
            claimed.append(
                {
                    "id": row.id,
                    "payload": dict(row.payload_json or {}),
                    "lease_token": token,
                    "attempt": row.publish_attempt_count,
                    "max_attempts": row.max_attempts,
                }
            )
        return claimed


def mark_outbox_published(event_id: str, lease_token: str) -> bool:
    now = datetime.now().astimezone()
    with session_scope() as session:
        event_id_value = session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == "publishing",
                OutboxEvent.lease_token == lease_token,
            )
            .values(
                status="published",
                published_at=now,
                claimed_by=None,
                lease_token=None,
                lease_expires_at=None,
                next_attempt_at=None,
                last_error=None,
            )
            .returning(OutboxEvent.id)
        ).scalar_one_or_none()
        return event_id_value is not None


def mark_outbox_failed(event_id: str, lease_token: str, error: str) -> str:
    now = datetime.now().astimezone()
    with session_scope() as session:
        row = session.execute(
            select(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == "publishing",
                OutboxEvent.lease_token == lease_token,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            return "stale"
        exhausted = row.publish_attempt_count >= max(1, int(row.max_attempts or OUTBOX_MAX_ATTEMPTS))
        row.status = "dead" if exhausted else "retry"
        row.failed_at = now if exhausted else None
        row.next_attempt_at = None if exhausted else now + timedelta(seconds=min(300, 2 ** max(0, row.publish_attempt_count - 1)))
        row.last_error = error[:2000]
        row.claimed_by = None
        row.lease_token = None
        row.lease_expires_at = None
        return row.status


def relay_outbox_batch(
    publisher: EventPublisher,
    *,
    limit: int = 50,
    event_ids: list[str] | None = None,
    worker_id: str | None = None,
) -> dict[str, int]:
    relay_id = worker_id or f"{socket.gethostname()}-outbox-{uuid.uuid4().hex[:8]}"
    events = claim_outbox_batch(worker_id=relay_id, limit=limit, event_ids=event_ids)
    report = {"published": 0, "failed": 0, "dead": 0, "stale": 0}
    for event in events:
        try:
            publisher.publish_task(str(event["id"]), dict(event["payload"]))
        except Exception as exc:
            status = mark_outbox_failed(
                str(event["id"]),
                str(event["lease_token"]),
                f"{type(exc).__name__}: {exc}",
            )
            if status == "stale":
                report["stale"] += 1
            elif status == "dead":
                report["dead"] += 1
                report["failed"] += 1
            else:
                report["failed"] += 1
            continue
        if mark_outbox_published(str(event["id"]), str(event["lease_token"])):
            report["published"] += 1
        else:
            report["stale"] += 1
    return report
