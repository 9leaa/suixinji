"""文件作用：Memory 读后写一致性屏障。

项目关系：本文件依赖 `core`、`repositories.postgres.dispatch`；被 `agent.query_agent`、`tests.test_memory_consistency_v3`。
"""



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
    """函数功能：`wait_for_memory_barrier` 负责等待 for memory barrier，服务于本文件职责：Memory 读后写一致性屏障。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        timeout_ms: timeout ms 参数，由调用方传入，类型为 `int | None`，默认值为 `None`。
        poll_interval_seconds: poll interval seconds 参数，由调用方传入，类型为 `float`，默认值为 `0.05`。
        progress_loader: progress loader 参数，由调用方传入，类型为 `Callable[[str], dict[str, int | None] | None] | None`，默认值为 `None`。
        sleep_fn: sleep fn 参数，由调用方传入，类型为 `Callable[[float], None]`，默认值为 `time.sleep`。
        monotonic_fn: monotonic fn 参数，由调用方传入，类型为 `Callable[[], float]`，默认值为 `time.monotonic`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
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
        """函数功能：`ready` 负责处理 ready，服务于本文件职责：Memory 读后写一致性屏障。
        传参：
            progress: progress 参数，由调用方传入，类型为 `dict[str, int | None]`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
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
