"""Retry provisional note enrichment without blocking interactive tasks."""

from __future__ import annotations

import logging
import threading

from core.settings import (
    ENRICHMENT_DRAIN_BATCH_SIZE,
    ENRICHMENT_DRAIN_INTERVAL_SECONDS,
    ENRICHMENT_MAX_ATTEMPTS,
)
from runtime.executor import BoundedTaskExecutor
from storage.note_storage import list_pending_enrichments


LOGGER = logging.getLogger(__name__)


class EnrichmentDrainer:
    def __init__(
        self,
        executor: BoundedTaskExecutor,
        *,
        interval_seconds: int = ENRICHMENT_DRAIN_INTERVAL_SECONDS,
        batch_size: int = ENRICHMENT_DRAIN_BATCH_SIZE,
    ) -> None:
        """初始化`EnrichmentDrainer` 实例并建立后续调用所需的状态。"""
        self._executor = executor
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def drain_once(self) -> int:
        """负责“清空once”。

        该函数是 `runtime.enrichment_drainer` 中的`EnrichmentDrainer` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        submitted = 0
        refs = list_pending_enrichments(
            limit=self._batch_size,
            max_attempts=ENRICHMENT_MAX_ATTEMPTS,
        )
        for ref in refs:
            if self._executor.submit_enrichment(ref["space_id"], ref["note_id"]):
                submitted += 1
        return submitted

    def start(self) -> None:
        """负责“启动”。

        该函数是 `runtime.enrichment_drainer` 中的`EnrichmentDrainer` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="suixinji-enrichment-drainer",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """负责“stop”。

        该函数是 `runtime.enrichment_drainer` 中的`EnrichmentDrainer` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1, self._interval_seconds))

    def _loop(self) -> None:
        """负责“loop”。

        该函数是 `runtime.enrichment_drainer` 中的`EnrichmentDrainer` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        LOGGER.info("Enrichment drainer started")
        while not self._stop_event.is_set():
            try:
                self.drain_once()
            except Exception:
                LOGGER.exception("Enrichment drainer tick failed")
            self._stop_event.wait(self._interval_seconds)
