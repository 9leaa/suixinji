"""文件作用：extraction state、partial/failed/empty。

项目关系：本文件依赖 `memory`、`memory.consolidator`、`memory.models`、`memory.repository`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from datetime import datetime, timedelta

import pytest

from memory import service
from memory.consolidator import process_unextracted_notes
from memory.models import MemoryCandidate
from memory.repository import (
    _connect,
    get_extraction_state,
    list_retryable_extraction_states,
    mark_extraction_completed,
    mark_extraction_failed,
    mark_extraction_processing,
)


def test_process_note_memory_marks_completed_and_empty(monkeypatch):
    """函数功能：`test_process_note_memory_marks_completed_and_empty` 负责验证 process note memory marks completed and empty 场景，服务于本文件职责：extraction state、partial/failed/empty。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(service, "extract_candidates", lambda note_id, text, classification=None: [])

    empty = service.process_note_memory({"id": "note-empty", "space_id": "space-1", "text": "你好"})

    assert empty["extraction_status"] == "empty"
    assert get_extraction_state("note-empty").status == "empty"

    candidate = MemoryCandidate("semantic", "用户正在学习 Agent", 0.8, 0.9)
    monkeypatch.setattr(service, "may_contain_memory", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(service, "extract_candidates", lambda note_id, text, classification=None: [candidate])
    monkeypatch.setattr(service, "consolidate_candidate", lambda space_id, note_id, candidate, trace=None: {"action": "insert"})

    completed = service.process_note_memory({"id": "note-ok", "space_id": "space-1", "text": "我正在学习 Agent"})

    state = get_extraction_state("note-ok")
    assert completed["extraction_status"] == "completed"
    assert state.status == "completed"
    assert state.candidate_count == 1
    assert state.processed_count == 1


def test_process_note_memory_marks_partial_and_failed(monkeypatch):
    """函数功能：`test_process_note_memory_marks_partial_and_failed` 负责验证 process note memory marks partial and failed 场景，服务于本文件职责：extraction state、partial/failed/empty。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    candidates = [
        MemoryCandidate("semantic", "用户学习 Agent", 0.8, 0.9),
        MemoryCandidate("preference", "用户喜欢咖啡", 0.7, 0.8),
    ]
    monkeypatch.setattr(service, "may_contain_memory", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(service, "extract_candidates", lambda note_id, text, classification=None: candidates)

    def partial_consolidate(space_id, note_id, candidate, trace=None):
        """函数功能：`partial_consolidate` 负责合并长期记忆 partial，服务于本文件职责：extraction state、partial/failed/empty。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
            note_id: Note 标识，用于定位原始记录。
            candidate: candidate 参数，由调用方传入。
            trace: trace 参数，由调用方传入，默认值为 `None`。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        if candidate.memory_type == "preference":
            raise RuntimeError("boom")
        return {"action": "insert", "memory_id": "mem-1"}

    monkeypatch.setattr(service, "consolidate_candidate", partial_consolidate)
    report = service.process_note_memory({"id": "note-partial", "space_id": "space-1", "text": "x"})

    state = get_extraction_state("note-partial")
    assert report["extraction_status"] == "partial"
    assert state.status == "partial"
    assert state.processed_count == 1
    assert "note-partial" in {item.note_id for item in list_retryable_extraction_states("space-1")}

    monkeypatch.setattr(service, "consolidate_candidate", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("all failed")))
    with pytest.raises(RuntimeError):
        service.process_note_memory({"id": "note-failed", "space_id": "space-1", "text": "x"})

    assert get_extraction_state("note-failed").status == "failed"
    assert "note-failed" in {item.note_id for item in list_retryable_extraction_states("space-1")}


def test_attempt_count_increments_on_processing():
    """函数功能：`test_attempt_count_increments_on_processing` 负责验证 attempt count increments on processing 场景，服务于本文件职责：extraction state、partial/failed/empty。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    first = mark_extraction_processing("note-1", "space-1")
    second = mark_extraction_processing("note-1", "space-1")

    assert first.attempt_count == 1
    assert second.attempt_count == 2


def test_daily_consolidation_uses_extraction_state(monkeypatch):
    """函数功能：`test_daily_consolidation_uses_extraction_state` 负责验证 daily consolidation uses extraction state 场景，服务于本文件职责：extraction state、partial/failed/empty。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    notes = [
        {"id": "note-completed", "space_id": "space-1", "text": "done"},
        {"id": "note-empty", "space_id": "space-1", "text": "empty"},
        {"id": "note-failed", "space_id": "space-1", "text": "failed"},
        {"id": "note-new", "space_id": "space-1", "text": "new"},
    ]
    mark_extraction_completed("note-completed", "space-1", candidate_count=1, processed_count=1)
    service.mark_extraction_empty("note-empty", "space-1")
    mark_extraction_failed("note-failed", "space-1", error="previous")
    processed = []

    monkeypatch.setattr("memory.consolidator.load_index", lambda space_id: notes)
    monkeypatch.setattr(
        service,
        "process_note_memory",
        lambda note: processed.append(note["id"]) or {"trace_id": "trace", "candidates": 1, "extraction_status": "completed"},
    )

    report = process_unextracted_notes("space-1")

    assert report["processed_count"] == 2
    assert processed == ["note-failed", "note-new"]


def test_stale_processing_is_retried(monkeypatch):
    """函数功能：`test_stale_processing_is_retried` 负责验证 stale processing is retried 场景，服务于本文件职责：extraction state、partial/failed/empty。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    mark_extraction_processing("note-stale", "space-1")
    old = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute("UPDATE memory_extraction_states SET updated_at = ? WHERE note_id = ?", (old, "note-stale"))
    processed = []

    monkeypatch.setattr("memory.consolidator.load_index", lambda space_id: [{"id": "note-stale", "space_id": "space-1", "text": "x"}])
    monkeypatch.setattr(
        service,
        "process_note_memory",
        lambda note: processed.append(note["id"]) or {"trace_id": "trace", "candidates": 1, "extraction_status": "completed"},
    )

    process_unextracted_notes("space-1")

    assert processed == ["note-stale"]
