"""文件作用：查询 memory barrier 与 V3 一致性。

项目关系：本文件依赖 `core`、`memory.consistency`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from memory.consistency import wait_for_memory_barrier


def test_memory_barrier_is_a_noop_when_feature_is_disabled(monkeypatch):
    """函数功能：`test_memory_barrier_is_a_noop_when_feature_is_disabled` 负责验证 memory barrier is a noop when feature is disabled 场景，服务于本文件职责：查询 memory barrier 与 V3 一致性。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from core import settings

    monkeypatch.setattr(settings, "QUERY_MEMORY_BARRIER_ENABLED", False)

    result = wait_for_memory_barrier("v3-space", progress_loader=lambda _: (_ for _ in ()).throw(AssertionError("must not read progress")))

    assert result == {"status": "skipped", "reason": "feature_disabled", "waited_ms": 0}


def test_memory_barrier_accepts_caught_up_watermarks(monkeypatch):
    """函数功能：`test_memory_barrier_accepts_caught_up_watermarks` 负责验证 memory barrier accepts caught up watermarks 场景，服务于本文件职责：查询 memory barrier 与 V3 一致性。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from core import settings

    monkeypatch.setattr(settings, "QUERY_MEMORY_BARRIER_ENABLED", True)
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "postgres")

    result = wait_for_memory_barrier(
        "v3-space",
        progress_loader=lambda _: {"note_watermark": 12, "memory_watermark": 12},
    )

    assert result["status"] == "ready"
    assert result["note_watermark"] == result["memory_watermark"] == 12
