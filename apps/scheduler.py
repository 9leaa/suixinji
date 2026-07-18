"""Leader-locked distributed Scheduler role."""

from __future__ import annotations

import logging
import time
from datetime import date

from core.observability import log_event
from core.settings import SCHEDULER_LEADER_TTL_MS, STAGE4_MODE
from infrastructure.redis_keys import KEYS
from infrastructure.redis_lock import RedisDistributedLock
from memory.repository import consolidation_period_key, flush_access_counts
from memory.scheduler import due_cadences, list_memory_space_ids
from repositories.postgres.dispatch import enqueue_task
from repositories.postgres.tasks import enqueue_due_retries
from runtime.stream_dispatcher import StreamTaskDispatcher
from summary.scheduler import run_scheduler_tick_safely

LOGGER = logging.getLogger(__name__)


def run_once() -> bool:
    lock = RedisDistributedLock(KEYS.lock_scheduler("distributed"), ttl_ms=SCHEDULER_LEADER_TTL_MS)
    if not lock.acquire(wait_seconds=0):
        return False
    try:
        try:
            flush_access_counts()
        except Exception:
            LOGGER.warning("memory access counter flush failed", exc_info=True)
        if STAGE4_MODE:
            log_event("runtime.scheduler_leader", status="completed", extra={"stage4_mode": True})
            return True
        run_scheduler_tick_safely(lambda _chat_id, _text: False, executor=StreamTaskDispatcher())
        today = date.today()
        for cadence in due_cadences(today, {}):
            period_key = consolidation_period_key(cadence, today)
            for space_id in list_memory_space_ids():
                enqueue_task(
                    task_type="memory",
                    space_id=space_id,
                    idempotency_key=f"memory:consolidate:{space_id}:{cadence}:{period_key}",
                    payload={"operation": "consolidate", "cadence": cadence, "period_key": period_key},
                )
        enqueue_due_retries()
        return True
    finally:
        lock.release()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            run_once()
        except Exception:
            LOGGER.exception("distributed scheduler tick failed")
        time.sleep(30)


if __name__ == "__main__":
    main()
