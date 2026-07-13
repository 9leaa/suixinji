"""Lightweight structured logging helpers for runtime observability."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

LOG_DIR = Path("data/logs")
_LOCK = threading.RLock()
LOGGER = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _duration_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _log_path() -> Path:
    return LOG_DIR / f"app-{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def log_event(
    action: str,
    *,
    level: str = "info",
    status: str = "success",
    space_id: str | None = None,
    message_id: str | None = None,
    record_id: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one JSONL event without breaking the business flow."""
    if os.getenv("SUIXINJI_OBSERVABILITY_DISABLED") == "1":
        return

    item = {
        "ts": now_iso(),
        "level": level,
        "action": action,
        "status": status,
        "space_id": space_id,
        "message_id": message_id,
        "record_id": record_id,
        "duration_ms": duration_ms,
        "error": error,
        "extra": extra or {},
    }

    try:
        with _LOCK:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with _log_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        LOGGER.exception("Failed to write observability event: %s", action)


@contextmanager
def observe(action: str, **ctx: Any) -> Iterator[None]:
    """Log start/success/failed events around a block."""
    start = time.perf_counter()
    ctx_extra = ctx.pop("extra", None) or {}
    log_event(action, status="start", extra=ctx_extra, **ctx)
    try:
        yield
    except Exception as exc:
        error_extra = dict(ctx_extra)
        error_extra["traceback"] = traceback.format_exc()
        log_event(
            action,
            level="error",
            status="failed",
            duration_ms=_duration_ms(start),
            error=f"{type(exc).__name__}: {exc}",
            extra=error_extra,
            **ctx,
        )
        raise
    else:
        log_event(action, status="success", duration_ms=_duration_ms(start), extra=ctx_extra, **ctx)


def read_recent_events(limit: int = 100) -> list[dict[str, Any]]:
    """Read recent structured log events from newest log files."""
    if not LOG_DIR.exists():
        return []

    events: list[dict[str, Any]] = []
    for path in sorted(LOG_DIR.glob("app-*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(events) >= limit:
                return events

    return events


def recent_errors(limit: int = 5) -> list[dict[str, Any]]:
    return [
        event
        for event in read_recent_events(limit=200)
        if event.get("level") == "error" or event.get("status") == "failed"
    ][:limit]


def latest_success(actions: set[str] | None = None) -> dict[str, Any] | None:
    for event in read_recent_events(limit=200):
        if event.get("status") != "success":
            continue
        if actions is not None and event.get("action") not in actions:
            continue
        return event
    return None
