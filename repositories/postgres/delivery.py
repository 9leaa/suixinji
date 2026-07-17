"""PostgreSQL delivery idempotency and lease repository."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from core.settings import DELIVERY_MAX_ATTEMPTS, DELIVERY_RESERVATION_TTL_SECONDS
from infrastructure.database import session_scope
from infrastructure.schema import Delivery, DeliveryAttempt
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space
from runtime.task import now_iso

DELIVERY_RESERVED = "reserved"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"
DELIVERY_UNKNOWN = "unknown"


def _record(row: Delivery):
    from runtime.delivery_store import DeliveryRecord
    return DeliveryRecord(
        delivery_key=row.delivery_key,
        delivery_type=row.delivery_type,
        space_id=row.space_id,
        message_id=row.message_id,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        reserved_at=row.reserved_at,
        lease_expires_at=row.lease_expires_at,
        attempt_count=row.attempt_count,
        error=row.error,
    )


def _future_iso(seconds: int) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def is_reservation_expired(record, now: datetime | None = None) -> bool:
    if record.status != DELIVERY_RESERVED:
        return False
    if not record.lease_expires_at:
        return True
    return _parse_iso(record.lease_expires_at) <= (now or datetime.now().astimezone())


def reserve_delivery(delivery_key: str, *, delivery_type: str, space_id: str, message_id: str | None = None):
    tenant_id = DEFAULT_TENANT_ID
    with session_scope() as session:
        ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:delivery_key))"), {"delivery_key": delivery_key})
        row = session.execute(
            select(Delivery).where(Delivery.delivery_key == delivery_key).with_for_update()
        ).scalar_one_or_none()
        if row is not None:
            current = _record(row)
            if current.status in {DELIVERY_SENT, DELIVERY_UNKNOWN}:
                return None
            if current.status == DELIVERY_RESERVED and not is_reservation_expired(current):
                return None
            if current.attempt_count >= DELIVERY_MAX_ATTEMPTS:
                return None

        now = now_iso()
        attempt_count = (row.attempt_count if row is not None else 0) + 1
        created_at = row.created_at if row is not None else now
        values = {
            "delivery_type": delivery_type,
            "tenant_id": tenant_id,
            "space_id": space_id,
            "message_id": message_id,
            "status": DELIVERY_RESERVED,
            "created_at": created_at,
            "updated_at": now,
            "reserved_at": now,
            "lease_expires_at": _future_iso(DELIVERY_RESERVATION_TTL_SECONDS),
            "attempt_count": attempt_count,
            "error": None,
        }
        if row is None:
            row = Delivery(delivery_key=delivery_key, **values)
            session.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        session.execute(
            insert(DeliveryAttempt)
            .values(
                delivery_key=delivery_key,
                attempt_no=attempt_count,
                status=DELIVERY_RESERVED,
                started_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_delivery_attempt_no")
        )
        session.flush()
        return _record(row)


def _update_status(delivery_key: str, status: str, error: str | None) -> None:
    now = now_iso()
    with session_scope() as session:
        row = session.execute(select(Delivery).where(Delivery.delivery_key == delivery_key).with_for_update()).scalar_one_or_none()
        if row is None:
            return
        row.status = status
        row.updated_at = now
        row.error = error
        session.execute(
            update(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_key == delivery_key, DeliveryAttempt.attempt_no == row.attempt_count)
            .values(status=status, finished_at=now, error=error)
        )


def mark_sent(delivery_key: str) -> None:
    _update_status(delivery_key, DELIVERY_SENT, None)


def mark_failed(delivery_key: str, error: str) -> None:
    _update_status(delivery_key, DELIVERY_FAILED, error)


def mark_unknown(delivery_key: str, error: str) -> None:
    _update_status(delivery_key, DELIVERY_UNKNOWN, error)


def get_delivery(delivery_key: str):
    with session_scope() as session:
        row = session.get(Delivery, delivery_key)
        return _record(row) if row is not None else None


def recover_stale_reserved_deliveries() -> int:
    now = datetime.now().astimezone()
    with session_scope() as session:
        rows = list(session.execute(select(Delivery).where(Delivery.status == DELIVERY_RESERVED).with_for_update()).scalars())
        stale = [row for row in rows if not row.lease_expires_at or _parse_iso(row.lease_expires_at) <= now]
        finished_at = now_iso()
        for row in stale:
            row.status = DELIVERY_FAILED
            row.updated_at = finished_at
            row.error = "reservation lease expired"
            session.execute(
                update(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_key == row.delivery_key, DeliveryAttempt.attempt_no == row.attempt_count)
                .values(status=DELIVERY_FAILED, finished_at=finished_at, error=row.error)
            )
        return len(stale)
