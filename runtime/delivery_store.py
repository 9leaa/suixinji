"""Local delivery idempotency store."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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
    error: str | None = None


def reserve_delivery(
    delivery_key: str,
    *,
    delivery_type: str,
    space_id: str,
    message_id: str | None = None,
) -> DeliveryRecord | None:
    """Reserve a send operation if it is safe to attempt."""
    with _LOCK:
        items = _load_raw()
        old = items.get(delivery_key)
        if old is not None and old.get("status") in {DELIVERY_SENT, DELIVERY_RESERVED, DELIVERY_UNKNOWN}:
            return None

        now = now_iso()
        record = DeliveryRecord(
            delivery_key=delivery_key,
            delivery_type=delivery_type,
            space_id=space_id,
            message_id=message_id,
            status=DELIVERY_RESERVED,
            created_at=str(old.get("created_at") if old else now),
            updated_at=now,
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
    return DeliveryRecord(**raw)


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


def ingest_archived_key(space_id: str, message_id: str) -> str:
    return f"ingest:{space_id}:{message_id}:archived"


def query_key(space_id: str, message_id: str) -> str:
    return f"query:{space_id}:{message_id}"


def manual_summary_key(space_id: str, message_id: str) -> str:
    return f"manual_summary:{space_id}:{message_id}"


def auto_summary_key(space_id: str, range_key: str, date: str) -> str:
    return f"auto_summary:{space_id}:{range_key}:{date}"
