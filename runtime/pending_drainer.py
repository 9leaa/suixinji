"""Background scanner that resubmits pending WAL records through the executor."""

from __future__ import annotations

import logging
import threading

from core.settings import PENDING_DRAIN_BATCH_SIZE, PENDING_DRAIN_INTERVAL_SECONDS
from core.wal import list_wal_space_ids, load_pending_records
from runtime.executor import BoundedTaskExecutor
from runtime.task import TASK_REJECTED


LOGGER = logging.getLogger(__name__)


class PendingDrainer:
    def __init__(
        self,
        executor: BoundedTaskExecutor,
        *,
        interval_seconds: int = PENDING_DRAIN_INTERVAL_SECONDS,
        batch_size: int = PENDING_DRAIN_BATCH_SIZE,
    ) -> None:
        self._executor = executor
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def drain_once(self) -> int:
        submitted = 0
        for space_id in list_wal_space_ids():
            records = load_pending_records(space_id)[: self._batch_size]
            for record in records:
                message_id = str(record.get("message_id") or "")
                if message_id and self._executor.has_inflight_ingest(space_id, message_id):
                    continue

                task = self._executor.submit_ingest(
                    record,
                    chat_id=None,
                    notify_on_success=False,
                    source="pending_drainer",
                )
                if task.status == TASK_REJECTED:
                    return submitted
                submitted += 1
        return submitted

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="suixinji-pending-drainer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1, self._interval_seconds))

    def _loop(self) -> None:
        LOGGER.info("Pending drainer started")
        while not self._stop_event.is_set():
            try:
                self.drain_once()
            except Exception:
                LOGGER.exception("Pending drainer tick failed")
            self._stop_event.wait(self._interval_seconds)
