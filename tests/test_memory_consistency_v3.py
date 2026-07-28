from __future__ import annotations

from memory.consistency import wait_for_memory_barrier


def test_memory_barrier_is_a_noop_when_feature_is_disabled(monkeypatch):
    """验证“记忆barrier是否为anoopwhenfeature是否为disabled”场景的预期行为与回归边界。"""
    from core import settings

    monkeypatch.setattr(settings, "QUERY_MEMORY_BARRIER_ENABLED", False)

    result = wait_for_memory_barrier("v3-space", progress_loader=lambda _: (_ for _ in ()).throw(AssertionError("must not read progress")))

    assert result == {"status": "skipped", "reason": "feature_disabled", "waited_ms": 0}


def test_memory_barrier_accepts_caught_up_watermarks(monkeypatch):
    """验证“记忆barrieracceptscaughtupwatermarks”场景的预期行为与回归边界。"""
    from core import settings

    monkeypatch.setattr(settings, "QUERY_MEMORY_BARRIER_ENABLED", True)
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "postgres")

    result = wait_for_memory_barrier(
        "v3-space",
        progress_loader=lambda _: {"note_watermark": 12, "memory_watermark": 12},
    )

    assert result["status"] == "ready"
    assert result["note_watermark"] == result["memory_watermark"] == 12
