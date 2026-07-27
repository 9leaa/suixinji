"""Bounded read-after-write barrier for Memory V3 queries."""

from __future__ import annotations

import time
from typing import Any, Callable

from core import settings


def wait_for_memory_barrier(
    space_id: str,
    *,
    timeout_ms: int | None = None,
    poll_interval_seconds: float = 0.05,
    progress_loader: Callable[[str], dict[str, int | None] | None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Wait briefly until memory catches the durable note watermark.

    This only runs for PostgreSQL deployments, has a hard default ceiling of
    800 ms, and never creates or alters data.  The caller can use the returned
    state to select the note fallback instead of reporting an empty answer.
    """
    if not settings.QUERY_MEMORY_BARRIER_ENABLED:
        return {"status": "skipped", "reason": "feature_disabled", "waited_ms": 0}
    if settings.STORAGE_BACKEND != "postgres":
        return {"status": "skipped", "reason": "non_postgres_backend", "waited_ms": 0}
    if progress_loader is None:
        from repositories.postgres.dispatch import get_space_progress

        progress_loader = get_space_progress

    timeout = max(0, min(int(timeout_ms if timeout_ms is not None else settings.QUERY_MEMORY_BARRIER_TIMEOUT_MS), 5_000))
    started = monotonic_fn()
    latest = progress_loader(space_id)
    if latest is None:
        return {"status": "skipped", "reason": "space_not_found", "waited_ms": 0}

    def ready(progress: dict[str, int | None]) -> bool:
        return int(progress.get("memory_watermark") or 0) >= int(progress.get("note_watermark") or 0)

    while not ready(latest):
        elapsed_ms = int((monotonic_fn() - started) * 1000)
        remaining_ms = timeout - elapsed_ms
        if remaining_ms <= 0:
            return {
                "status": "timeout",
                "reason": "memory_watermark_lagging",
                "waited_ms": min(timeout, elapsed_ms),
                "note_watermark": int(latest.get("note_watermark") or 0),
                "memory_watermark": int(latest.get("memory_watermark") or 0),
            }
        sleep_fn(min(max(0.001, poll_interval_seconds), remaining_ms / 1000))
        refreshed = progress_loader(space_id)
        if refreshed is None:
            return {"status": "skipped", "reason": "space_not_found", "waited_ms": int((monotonic_fn() - started) * 1000)}
        latest = refreshed

    return {
        "status": "ready",
        "waited_ms": int((monotonic_fn() - started) * 1000),
        "note_watermark": int(latest.get("note_watermark") or 0),
        "memory_watermark": int(latest.get("memory_watermark") or 0),
    }
