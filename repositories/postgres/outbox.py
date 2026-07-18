"""Transactional Outbox relay repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select

from infrastructure.database import session_scope
from infrastructure.schema import OutboxEvent


class EventPublisher(Protocol):
    def publish_task(self, event_id: str, payload: dict[str, Any]) -> str: ...


def relay_outbox_batch(
    publisher: EventPublisher,
    *,
    limit: int = 50,
    event_ids: list[str] | None = None,
) -> dict[str, int]:
    published = 0
    failed = 0
    with session_scope() as session:
        statement = select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
        if event_ids is not None:
            statement = statement.where(OutboxEvent.id.in_(event_ids))
        rows = list(session.execute(
            statement.order_by(OutboxEvent.created_at).limit(max(1, int(limit))).with_for_update(skip_locked=True)
        ).scalars())
        for row in rows:
            try:
                publisher.publish_task(row.id, dict(row.payload_json or {}))
            except Exception as exc:
                row.publish_attempt_count += 1
                row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                failed += 1
                continue
            row.published_at = datetime.now().astimezone()
            row.publish_attempt_count += 1
            row.last_error = None
            published += 1
    return {"published": published, "failed": failed}
