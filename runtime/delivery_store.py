"""Local delivery idempotency store."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.observability import log_event
from core.settings import DELIVERY_MAX_ATTEMPTS, DELIVERY_RESERVATION_TTL_SECONDS
from runtime.task import now_iso


DATA_DIR = Path("data")
DELIVERY_DIR = DATA_DIR / "deliveries"
DELIVERY_PATH = DELIVERY_DIR / "index.json"

DELIVERY_RESERVED = "reserved"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"
DELIVERY_UNKNOWN = "unknown"

_LOCK = threading.RLock()


@dataclass
class DeliveryRecord:
    delivery_key: str
    delivery_type: str
    space_id: str
    message_id: str | None
    status: str
    created_at: str
    updated_at: str
    reserved_at: str | None = None
    lease_expires_at: str | None = None
    attempt_count: int = 0
    error: str | None = None


def reserve_delivery(
    delivery_key: str,
    *,
    delivery_type: str,
    space_id: str,
    message_id: str | None = None,
    tenant_id: str = "default",
) -> DeliveryRecord | None:
    """Reserve a send operation if it is safe to attempt."""
    del tenant_id
    with _LOCK:
        items = _load_raw()
        old = items.get(delivery_key)
        if old is not None:
            old_record = _record_from_raw(old)
            if old_record.status in {DELIVERY_SENT, DELIVERY_UNKNOWN}:
                return None
            if old_record.status == DELIVERY_RESERVED and not is_reservation_expired(old_record):
                return None
            if old_record.attempt_count >= DELIVERY_MAX_ATTEMPTS:
                log_event(
                    "runtime.delivery_exhausted",
                    level="warning",
                    status="skipped",
                    space_id=old_record.space_id,
                    message_id=old_record.message_id,
                    error="delivery max attempts exhausted",
                    extra={"delivery_key": delivery_key, "attempt_count": old_record.attempt_count},
                )
                return None

        now = now_iso()
        attempt_count = 1
        if old is not None:
            attempt_count = _record_from_raw(old).attempt_count + 1
        record = DeliveryRecord(
            delivery_key=delivery_key,
            delivery_type=delivery_type,
            space_id=space_id,
            message_id=message_id,
            status=DELIVERY_RESERVED,
            created_at=str(old.get("created_at") if old else now),
            updated_at=now,
            reserved_at=now,
            lease_expires_at=_future_iso(DELIVERY_RESERVATION_TTL_SECONDS),
            attempt_count=attempt_count,
            error=None,
        )
        items[delivery_key] = asdict(record)
        _save_raw(items)
        return record


def mark_sent(delivery_key: str) -> None:
    _update_status(delivery_key, DELIVERY_SENT, None)


def mark_failed(delivery_key: str, error: str) -> None:
    _update_status(delivery_key, DELIVERY_FAILED, error)


def mark_unknown(delivery_key: str, error: str) -> None:
    _update_status(delivery_key, DELIVERY_UNKNOWN, error)


def get_delivery(delivery_key: str) -> DeliveryRecord | None:
    raw = _load_raw().get(delivery_key)
    if raw is None:
        return None
    return _record_from_raw(raw)


def is_reservation_expired(record: DeliveryRecord, now: datetime | None = None) -> bool:
    if record.status != DELIVERY_RESERVED:
        return False
    if not record.lease_expires_at:
        return True
    now = now or datetime.now().astimezone()
    return _parse_iso(record.lease_expires_at) <= now


def recover_stale_reserved_deliveries() -> int:
    """Mark expired reserved deliveries as failed so they can be retried later."""
    recovered = 0
    with _LOCK:
        items = _load_raw()
        for key, raw in list(items.items()):
            record = _record_from_raw(raw)
            if record.status != DELIVERY_RESERVED or not is_reservation_expired(record):
                continue
            record.status = DELIVERY_FAILED
            record.updated_at = now_iso()
            record.error = "reservation lease expired"
            items[key] = asdict(record)
            recovered += 1
            log_event(
                "runtime.delivery_stale_reserved",
                level="warning",
                status="failed",
                space_id=record.space_id,
                message_id=record.message_id,
                error=record.error,
                extra={"delivery_key": key, "attempt_count": record.attempt_count},
            )
        if recovered:
            _save_raw(items)
    return recovered


def _update_status(delivery_key: str, status: str, error: str | None) -> None:
    with _LOCK:
        items = _load_raw()
        raw = items.get(delivery_key)
        if raw is None:
            return
        raw["status"] = status
        raw["updated_at"] = now_iso()
        raw["error"] = error
        items[delivery_key] = raw
        _save_raw(items)


def _load_raw() -> dict[str, dict[str, Any]]:
    with _LOCK:
        if not DELIVERY_PATH.exists():
            return {}
        return json.loads(DELIVERY_PATH.read_text(encoding="utf-8"))


def _save_raw(items: dict[str, dict[str, Any]]) -> None:
    with _LOCK:
        DELIVERY_DIR.mkdir(parents=True, exist_ok=True)
        DELIVERY_PATH.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _record_from_raw(raw: dict[str, Any]) -> DeliveryRecord:
    return DeliveryRecord(
        delivery_key=str(raw["delivery_key"]),
        delivery_type=str(raw["delivery_type"]),
        space_id=str(raw["space_id"]),
        message_id=raw.get("message_id"),
        status=str(raw["status"]),
        created_at=str(raw["created_at"]),
        updated_at=str(raw["updated_at"]),
        reserved_at=raw.get("reserved_at"),
        lease_expires_at=raw.get("lease_expires_at"),
        attempt_count=int(raw.get("attempt_count") or 0),
        error=raw.get("error"),
    )


def _future_iso(seconds: int) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def ingest_archived_key(space_id: str, message_id: str) -> str:
    return f"ingest:{space_id}:{message_id}:archived"


def query_key(space_id: str, message_id: str) -> str:
    return f"query:{space_id}:{message_id}"


def manual_summary_key(space_id: str, message_id: str) -> str:
    return f"manual_summary:{space_id}:{message_id}"


def auto_summary_key(space_id: str, range_key: str, date: str) -> str:
    return f"auto_summary:{space_id}:{range_key}:{date}"


from core.settings import STORAGE_BACKEND as _STORAGE_BACKEND

if _STORAGE_BACKEND == "postgres":
    from repositories.postgres import delivery as _postgres_delivery

    reserve_delivery = _postgres_delivery.reserve_delivery
    mark_sent = _postgres_delivery.mark_sent
    mark_failed = _postgres_delivery.mark_failed
    mark_unknown = _postgres_delivery.mark_unknown
    get_delivery = _postgres_delivery.get_delivery
    is_reservation_expired = _postgres_delivery.is_reservation_expired
    recover_stale_reserved_deliveries = _postgres_delivery.recover_stale_reserved_deliveries
