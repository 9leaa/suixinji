"""文件作用：本地富化 drain。

项目关系：本文件依赖 `core.settings`、`runtime.executor`、`storage.note_storage`；被 `bot.feishu_bot`。
"""



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
    """类功能：`EnrichmentDrainer` 封装与“本地富化 drain”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(
        self,
        executor: BoundedTaskExecutor,
        *,
        interval_seconds: int = ENRICHMENT_DRAIN_INTERVAL_SECONDS,
        batch_size: int = ENRICHMENT_DRAIN_BATCH_SIZE,
    ) -> None:
        """函数功能：`EnrichmentDrainer.__init__` 在类 `EnrichmentDrainer` 中负责初始化实例状态，服务于本文件职责：本地富化 drain。
        传参：
            executor: executor 参数，由调用方传入，类型为 `BoundedTaskExecutor`。
            interval_seconds: interval seconds 参数，由调用方传入，类型为 `int`，默认值为 `ENRICHMENT_DRAIN_INTERVAL_SECONDS`。
            batch_size: batch size 参数，由调用方传入，类型为 `int`，默认值为 `ENRICHMENT_DRAIN_BATCH_SIZE`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._executor = executor
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def drain_once(self) -> int:
        """函数功能：`EnrichmentDrainer.drain_once` 在类 `EnrichmentDrainer` 中负责清空待处理队列 once，服务于本文件职责：本地富化 drain。
        传参：
            无。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
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
        """函数功能：`EnrichmentDrainer.start` 在类 `EnrichmentDrainer` 中负责启动，服务于本文件职责：本地富化 drain。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
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
        """函数功能：`EnrichmentDrainer.stop` 在类 `EnrichmentDrainer` 中负责停止，服务于本文件职责：本地富化 drain。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1, self._interval_seconds))

    def _loop(self) -> None:
        """函数功能：`EnrichmentDrainer._loop` 在类 `EnrichmentDrainer` 中负责处理 loop，服务于本文件职责：本地富化 drain。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        LOGGER.info("Enrichment drainer started")
        while not self._stop_event.is_set():
            try:
                self.drain_once()
            except Exception:
                LOGGER.exception("Enrichment drainer tick failed")
            self._stop_event.wait(self._interval_seconds)
