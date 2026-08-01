"""文件作用：Outbox 到 Redis Stream 的 relay 进程。

项目关系：本文件依赖 `core.observability`、`core.settings`、`repositories.postgres.outbox`、`repositories.postgres.tasks` 等 5 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""



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
    """函数功能：`run_once` 负责运行 once，服务于本文件职责：Outbox 到 Redis Stream 的 relay 进程。
    传参：
        client: 外部服务或基础设施客户端，类型为 `StreamClient | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, int]`，表示结构化结果、载荷或状态映射。
    """
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
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：Outbox 到 Redis Stream 的 relay 进程。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
