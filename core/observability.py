"""Lightweight structured logging helpers for runtime observability."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from core.sensitive import assess_sensitive_text, redact_sensitive_text

LOG_DIR = Path("data/logs")
_LOCK = threading.RLock()
LOGGER = logging.getLogger(__name__)


def _safe_log_value(value: Any) -> Any:
    if isinstance(value, str):
        if assess_sensitive_text(value).blocks_storage:
            return "[sensitive content redacted]"
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {str(key): _safe_log_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_log_value(item) for item in value]
    return value


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
        "error": _safe_log_value(error),
        "extra": _safe_log_value(extra or {}),
    }

    try:
        with _LOCK:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with _log_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        LOGGER.exception("Failed to write observability event: %s", action)


def _code_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    value = result.stdout.strip()
    return value or None


def log_process_started(role: str | None = None, *, action: str = "runtime.process_started") -> None:
    from core.settings import (
        PROCESS_ROLE,
        REDIS_BLOCKING_SOCKET_TIMEOUT_SECONDS,
        REDIS_SOCKET_TIMEOUT_SECONDS,
        STREAM_BLOCK_MS,
        database_pool_budget,
    )
    from infrastructure.redis_keys import KEYS

    resolved_role = role or PROCESS_ROLE or "default"
    pool_size, max_overflow = database_pool_budget(resolved_role)
    log_event(
        action,
        status="started",
        extra={
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_role": PROCESS_ROLE,
            "role": resolved_role,
            "database_pool_size": pool_size,
            "database_max_overflow": max_overflow,
            "redis_namespace": KEYS.prefix,
            "stream_block_ms": STREAM_BLOCK_MS,
            "redis_socket_timeout_seconds": REDIS_SOCKET_TIMEOUT_SECONDS,
            "redis_blocking_socket_timeout_seconds": REDIS_BLOCKING_SOCKET_TIMEOUT_SECONDS,
            "code_revision": _code_revision(),
            "start_time": now_iso(),
        },
    )


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
