"""文件作用：本地 WAL 定期 drain。

项目关系：本文件依赖 `core.settings`、`core.wal`、`runtime.executor`、`runtime.task`；被 `bot.feishu_bot`、`tests.test_pending_drainer`。
"""



from __future__ import annotations

import logging
import threading

from core.settings import PENDING_DRAIN_BATCH_SIZE, PENDING_DRAIN_INTERVAL_SECONDS
from core.wal import list_wal_space_ids, load_pending_records
from runtime.executor import BoundedTaskExecutor
from runtime.task import TASK_REJECTED


LOGGER = logging.getLogger(__name__)


class PendingDrainer:
    """类功能：`PendingDrainer` 封装与“本地 WAL 定期 drain”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(
        self,
        executor: BoundedTaskExecutor,
        *,
        interval_seconds: int = PENDING_DRAIN_INTERVAL_SECONDS,
        batch_size: int = PENDING_DRAIN_BATCH_SIZE,
    ) -> None:
        """函数功能：`PendingDrainer.__init__` 在类 `PendingDrainer` 中负责初始化实例状态，服务于本文件职责：本地 WAL 定期 drain。
        传参：
            executor: executor 参数，由调用方传入，类型为 `BoundedTaskExecutor`。
            interval_seconds: interval seconds 参数，由调用方传入，类型为 `int`，默认值为 `PENDING_DRAIN_INTERVAL_SECONDS`。
            batch_size: batch size 参数，由调用方传入，类型为 `int`，默认值为 `PENDING_DRAIN_BATCH_SIZE`。
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
        """函数功能：`PendingDrainer.drain_once` 在类 `PendingDrainer` 中负责清空待处理队列 once，服务于本文件职责：本地 WAL 定期 drain。
        传参：
            无。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
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
        """函数功能：`PendingDrainer.start` 在类 `PendingDrainer` 中负责启动，服务于本文件职责：本地 WAL 定期 drain。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="suixinji-pending-drainer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """函数功能：`PendingDrainer.stop` 在类 `PendingDrainer` 中负责停止，服务于本文件职责：本地 WAL 定期 drain。
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
        """函数功能：`PendingDrainer._loop` 在类 `PendingDrainer` 中负责处理 loop，服务于本文件职责：本地 WAL 定期 drain。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        LOGGER.info("Pending drainer started")
        while not self._stop_event.is_set():
            try:
                self.drain_once()
            except Exception:
                LOGGER.exception("Pending drainer tick failed")
            self._stop_event.wait(self._interval_seconds)
