from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from core import settings
from memory import extractor, repository
from memory.canonicalizer import canonicalize_candidate
from memory.consolidator import consolidate_candidate
from memory.models import MemoryCandidate


def enable_stage2(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_CANONICAL_KEY_V3_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_RELATION_GUARD_V3_ENABLED", True)
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "rules")


def candidate(space: str, note: str, text: str, entity: str, attribute: str, operation: str, status: str) -> MemoryCandidate:
    topic = operation + ("" if entity == "用户" else entity) + attribute
    return canonicalize_candidate(
        MemoryCandidate(
            "task", text, 0.9, 0.95,
            task_status=status, note_id=note, space_id=space,
            subject=entity, predicate=attribute, object_value=attribute,
            evidence_span=text,
            scope={"operation": operation, "canonical_topic": topic, "scope": "global"},
        )
    )


def test_todo_to_done_keeps_identity_and_creates_version(monkeypatch):
    enable_stage2(monkeypatch)
    space = "stage2-todo"
    old = repository.insert_memory(space, candidate(space, "old", "修复登录接口超时问题", "随心记", "登录接口超时问题", "修复", "todo"), source_note_id="old")
    report = consolidate_candidate(space, "done", candidate(space, "done", "登录接口超时问题已经修复", "随心记", "登录接口超时问题", "修复", "done"))
    current = repository.get_memory(old.id)
    assert report["action"] == "update_task"
    assert current is not None and current.id == old.id and current.memory_key == old.memory_key
    assert current.task_status == "done" and current.current_version == 2
    assert [version.task_status for version in current.versions] == ["todo", "done"]
    assert {source.note_id for source in current.sources} == {"old", "done"}


def test_done_done_is_same_and_idempotent(monkeypatch):
    enable_stage2(monkeypatch)
    space = "stage2-done-idempotent"
    first = candidate(space, "todo", "记得完成测试报告", "随心记", "测试报告", "完成", "todo")
    repository.insert_memory(space, first, source_note_id="todo")
    done = candidate(space, "done", "测试报告已经完成", "随心记", "测试报告", "完成", "done")
    first_report = consolidate_candidate(space, "done", done)
    second_report = consolidate_candidate(space, "done", done)
    current = repository.get_memory(first_report["memory_id"])
    assert first_report["action"] == "update_task"
    assert second_report["idempotent"] is True
    assert current is not None and current.task_status == "done" and current.current_version == 2
    assert {source.note_id for source in current.sources} == {"todo", "done"}


def test_cancelled_done_is_pending_review(monkeypatch):
    enable_stage2(monkeypatch)
    space = "stage2-cancelled"
    cancelled = candidate(space, "cancelled", "取消测试报告", "随心记", "测试报告", "完成", "done")
    cancelled = MemoryCandidate(**{**cancelled.__dict__, "scope": {**cancelled.scope, "closure_reason": "cancelled"}})
    old = repository.insert_memory(space, cancelled, source_note_id="cancelled")
    report = consolidate_candidate(space, "done", candidate(space, "done", "测试报告已经完成", "随心记", "测试报告", "完成", "done"))
    assert report["action"] == "pending_review"
    assert report["relation"] == "conflict"
    assert repository.get_memory(old.id).task_status == "done"
    pending = repository.list_memories(space, status="pending_review", memory_type="task")
    assert len(pending) == 1 and pending[0].task_status == "done"


def test_orphan_completion_stays_done_task(monkeypatch):
    enable_stage2(monkeypatch)
    space = "stage2-orphan-event"
    report = consolidate_candidate(space, "event", candidate(space, "event", "我昨天提交了论文", "用户", "论文", "提交", "done"))
    assert report["relation"] == "new" and report["action"] == "insert"
    memories = repository.list_memories(space, status="active")
    assert len(memories) == 1 and memories[0].memory_type == "task"
    assert memories[0].task_status == "done"
    assert not repository.list_memories(space, status="active", memory_type="episodic")


def test_orphan_strong_completion_can_create_explicit_done_task(monkeypatch):
    enable_stage2(monkeypatch)
    space = "stage2-orphan-strong"
    report = consolidate_candidate(space, "strong", candidate(space, "strong", "随心记第一阶段评测已经完成", "随心记", "第一阶段评测", "完善", "done"))
    assert report["relation"] == "new" and report["action"] == "insert"
    active = repository.list_memories(space, status="active", memory_type="task")
    assert len(active) == 1 and active[0].task_status == "done"


def test_multiple_history_matches_are_ambiguous(monkeypatch):
    enable_stage2(monkeypatch)
    space = "stage2-ambiguous"
    for index in (1, 2):
        repository.insert_memory(space, candidate(space, f"todo-{index}", "记得完成测试报告", "随心记", "测试报告", "完成", "todo"), source_note_id=f"todo-{index}")
    report = consolidate_candidate(space, "done", candidate(space, "done", "测试报告已经完成", "随心记", "测试报告", "完成", "done"))
    assert report["relation"] == "ambiguous_match" and report["action"] == "pending_review"
    assert len(report["target_memory_ids"]) == 2
    assert len(repository.list_memories(space, status="active", memory_type="task")) == 2


def test_two_workers_do_not_create_two_done_versions(monkeypatch):
    enable_stage2(monkeypatch)
    space = "stage2-concurrent"
    old = repository.insert_memory(space, candidate(space, "todo", "记得完成测试报告", "随心记", "测试报告", "完成", "todo"), source_note_id="todo")
    done = candidate(space, "done", "测试报告已经完成", "随心记", "测试报告", "完成", "done")
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: consolidate_candidate(space, "done", done), (1, 2)))
    current = repository.get_memory(old.id)
    assert current is not None and current.task_status == "done" and current.current_version == 2
    assert len(current.versions) == 2
    assert any(report.get("idempotent") for report in reports)
