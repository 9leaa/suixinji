"""P4 automatic summary subscription storage."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DATA_DIR = Path("data")
SUBSCRIPTIONS_PATH = DATA_DIR / "summary_subscriptions.json"
DEFAULT_SUMMARY_TIME = "22:00"
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_LOCK = threading.RLock()


@dataclass
class SummarySubscription:
    space_id: str
    chat_id: str
    enabled: bool = True
    time: str = DEFAULT_SUMMARY_TIME
    range_key: str = "today"
    last_sent_date: str | None = None


def parse_summary_time(value: str) -> str | None:
    value = value.strip()
    if not _TIME_RE.match(value):
        return None
    return value


def _load_raw() -> dict[str, dict[str, Any]]:
    with _LOCK:
        if not SUBSCRIPTIONS_PATH.exists():
            return {}
        return json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))


def _save_raw(items: dict[str, dict[str, Any]]) -> None:
    with _LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SUBSCRIPTIONS_PATH.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def get_summary_subscription(space_id: str) -> SummarySubscription | None:
    raw = _load_raw().get(space_id)
    if raw is None:
        return None
    return SummarySubscription(**raw)


def list_enabled_summary_subscriptions() -> list[SummarySubscription]:
    return [
        SummarySubscription(**item)
        for item in _load_raw().values()
        if item.get("enabled")
    ]


def enable_summary_subscription(space_id: str, chat_id: str) -> SummarySubscription:
    items = _load_raw()
    old = items.get(space_id, {})
    sub = SummarySubscription(
        space_id=space_id,
        chat_id=chat_id,
        enabled=True,
        time=str(old.get("time") or DEFAULT_SUMMARY_TIME),
        range_key=str(old.get("range_key") or "today"),
        last_sent_date=old.get("last_sent_date"),
    )
    items[space_id] = asdict(sub)
    _save_raw(items)
    return sub


def disable_summary_subscription(space_id: str) -> SummarySubscription | None:
    items = _load_raw()
    old = items.get(space_id)
    if old is None:
        return None
    old["enabled"] = False
    items[space_id] = old
    _save_raw(items)
    return SummarySubscription(**old)


def update_summary_time(space_id: str, chat_id: str, time_value: str) -> SummarySubscription:
    parsed = parse_summary_time(time_value)
    if parsed is None:
        raise ValueError("time must be HH:MM, for example 22:00")

    items = _load_raw()
    old = items.get(space_id, {})
    sub = SummarySubscription(
        space_id=space_id,
        chat_id=chat_id,
        enabled=bool(old.get("enabled", True)),
        time=parsed,
        range_key=str(old.get("range_key") or "today"),
        last_sent_date=old.get("last_sent_date"),
    )
    items[space_id] = asdict(sub)
    _save_raw(items)
    return sub


def mark_summary_sent(space_id: str, sent_date: str) -> None:
    items = _load_raw()
    old = items.get(space_id)
    if old is None:
        return
    old["last_sent_date"] = sent_date
    items[space_id] = old
    _save_raw(items)