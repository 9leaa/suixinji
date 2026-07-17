"""Continuously relay PostgreSQL Outbox events to Redis Streams."""

from __future__ import annotations

import logging
import time

from core.settings import OUTBOX_BATCH_SIZE, OUTBOX_POLL_INTERVAL_SECONDS
from repositories.postgres.outbox import relay_outbox_batch
from repositories.postgres.tasks import enqueue_due_retries
from runtime.streams import StreamClient

LOGGER = logging.getLogger(__name__)


def run_once(client: StreamClient | None = None) -> dict[str, int]:
    enqueue_due_retries(limit=OUTBOX_BATCH_SIZE)
    return relay_outbox_batch(client or StreamClient(), limit=OUTBOX_BATCH_SIZE)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    client = StreamClient()
    while True:
        report = run_once(client)
        if report["failed"]:
            LOGGER.warning("outbox relay failures: %s", report)
        if report["published"] == 0:
            time.sleep(max(0.1, OUTBOX_POLL_INTERVAL_SECONDS))


if __name__ == "__main__":
    main()
