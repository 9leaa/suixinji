"""文件作用：分布式 scheduler 入口。

项目关系：本文件依赖 `core.observability`、`core.settings`、`infrastructure.redis_keys`、`infrastructure.redis_lock` 等 10 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import logging
import time
from datetime import date

from core.observability import log_event, log_process_started
from core.settings import SCHEDULER_LEADER_TTL_MS, STAGE4_MODE
from infrastructure.redis_keys import KEYS
from infrastructure.redis_lock import RedisDistributedLock
from memory.repository import consolidation_period_key, flush_access_counts
from memory.scheduler import due_cadences, is_evaluation_space, list_memory_space_ids
from repositories.postgres.dispatch import enqueue_task
from repositories.postgres.tasks import enqueue_due_retries
from repositories.postgres.semantic_profile_projection import (
    enqueue_stale_semantic_profile_projection_rebuilds,
    enqueue_uninitialized_semantic_profile_projection_rebuilds,
)
from runtime.stream_dispatcher import StreamTaskDispatcher
from summary.scheduler import run_scheduler_tick_safely

LOGGER = logging.getLogger(__name__)


def run_once() -> bool:
    """函数功能：`run_once` 负责运行 once，服务于本文件职责：分布式 scheduler 入口。
    传参：
        无。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    lock = RedisDistributedLock(KEYS.lock_scheduler("distributed"), ttl_ms=SCHEDULER_LEADER_TTL_MS)
    if not lock.acquire(wait_seconds=0):
        return False
    try:
        try:
            flush_access_counts()
        except Exception:
            LOGGER.warning("memory access counter flush failed", exc_info=True)
        try:
            initialized = enqueue_uninitialized_semantic_profile_projection_rebuilds()
            rebuilt = enqueue_stale_semantic_profile_projection_rebuilds()
            if initialized or rebuilt:
                log_event(
                    "semantic_profile_projection_rebuild_queued",
                    status="queued",
                    extra={"initialized_count": initialized, "expired_count": rebuilt},
                )
        except Exception:
            LOGGER.warning("semantic profile projection rebuild scan failed", exc_info=True)
        if STAGE4_MODE:
            log_event("runtime.scheduler_leader", status="completed", extra={"stage4_mode": True})
            return True
        run_scheduler_tick_safely(lambda _chat_id, _text: False, executor=StreamTaskDispatcher())
        today = date.today()
        for cadence in due_cadences(today, {}):
            period_key = consolidation_period_key(cadence, today)
            for space_id in list_memory_space_ids():
                # Layer evaluation spaces contain real-looking seed Notes, but
                # are test fixtures and must never enter the distributed
                # consolidation stream.
                if is_evaluation_space(space_id):
                    continue
                enqueue_task(
                    task_type="memory",
                    space_id=space_id,
                    idempotency_key=f"memory:consolidate:{space_id}:{cadence}:{period_key}",
                    payload={"operation": "consolidate", "cadence": cadence, "period_key": period_key},
                )
        retry_count = enqueue_due_retries()
        if retry_count:
            log_event(
                "runtime.task_retry_published",
                status="queued",
                extra={"retry_count": retry_count, "source": "scheduler"},
            )
        return True
    finally:
        lock.release()


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：分布式 scheduler 入口。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    logging.basicConfig(level=logging.INFO)
    log_process_started("scheduler")
    while True:
        try:
            run_once()
        except Exception:
            LOGGER.exception("distributed scheduler tick failed")
        time.sleep(30)


if __name__ == "__main__":
    main()
