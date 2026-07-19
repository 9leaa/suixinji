"""Continuously relay PostgreSQL Outbox events to Redis Streams."""

from __future__ import annotations

import logging
import time

from core.observability import log_event, log_process_started
from core.settings import OUTBOX_BATCH_SIZE, OUTBOX_POLL_INTERVAL_SECONDS
from repositories.postgres.outbox import relay_outbox_batch
from repositories.postgres.tasks import enqueue_due_retries
from runtime.streams import StreamClient

LOGGER = logging.getLogger(__name__)


def run_once(client: StreamClient | None = None) -> dict[str, int]:
    retry_count = enqueue_due_retries(limit=OUTBOX_BATCH_SIZE)
    if retry_count:
        log_event(
            "runtime.task_retry_published",
            status="queued",
            extra={"retry_count": retry_count, "source": "outbox-relay"},
        )
    started = time.perf_counter()
    report = relay_outbox_batch(client or StreamClient(), limit=OUTBOX_BATCH_SIZE)
    if any(report.values()):
        log_event(
            "runtime.outbox_relay",
            level="warning" if report["failed"] or report["dead"] or report["stale"] else "info",
            status="partial" if report["failed"] or report["dead"] or report["stale"] else "completed",
            duration_ms=int((time.perf_counter() - started) * 1000),
            extra=report,
        )
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log_process_started("outbox-relay")
    client = StreamClient()
    while True:
        report = run_once(client)
        if report["failed"]:
            LOGGER.warning("outbox relay failures: %s", report)
        if report["published"] == 0:
            time.sleep(max(0.1, OUTBOX_POLL_INTERVAL_SECONDS))


if __name__ == "__main__":
    main()
