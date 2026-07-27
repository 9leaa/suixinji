from __future__ import annotations

from memory import extractor
from memory import repository
from memory.models import MemoryCandidate, MemoryDecision
from memory.service import process_note_memory


def _enable_task_identity(monkeypatch) -> None:
    from core import settings

    monkeypatch.setattr(settings, "MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_CANONICAL_KEY_V3_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_RELATION_GUARD_V3_ENABLED", True)
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "rules")


def test_task_lifecycle_merges_action_wording(monkeypatch) -> None:
    _enable_task_identity(monkeypatch)
    texts = (
        "记得制作随心记首页的消息路径图",
        "正在制作随心记首页的消息路径图",
        "我首页消息路径图也做完了",
    )
    for index, text in enumerate(texts):
        process_note_memory({"id": f"task-wording-{index}", "space_id": "task-wording", "text": text})

    memories = repository.list_memories("task-wording", memory_type="task", limit=10)
    assert len(memories) == 1
    assert memories[0].task_status == "done"
    assert memories[0].current_version == 3


def test_legacy_generic_task_identity_is_updated(monkeypatch) -> None:
    _enable_task_identity(monkeypatch)
    legacy = repository.insert_memory(
        "task-legacy",
        MemoryCandidate(
            "task",
            "制作随心记首页的消息路径图",
            0.8,
            0.9,
            subject="制作随心记首页的消息路径图",
            predicate="task",
            object_value="制作随心记首页的消息路径图",
            task_status="todo",
        ),
        source_note_id="legacy-source",
    )
    result = process_note_memory({"id": "legacy-done", "space_id": "task-legacy", "text": "我首页消息路径图也做完了"})

    assert result["results"][0]["action"] == "update_task"
    memories = repository.list_memories("task-legacy", memory_type="task", limit=10)
    assert len(memories) == 1
    assert memories[0].id == legacy.id
    assert memories[0].task_status == "done"


def test_terminal_update_archives_duplicate_active_tasks(monkeypatch) -> None:
    _enable_task_identity(monkeypatch)
    for index, text in enumerate(("制作随心记首页的消息路径图", "正在制作随心记首页消息路径图")):
        repository.insert_memory(
            "task-duplicates",
            MemoryCandidate(
                "task", text, 0.8, 0.9,
                subject="制作随心记首页消息路径图", predicate="task", object_value=text,
                task_status="in_progress",
            ),
            source_note_id=f"duplicate-source-{index}",
        )

    result = process_note_memory(
        {"id": "duplicate-done", "space_id": "task-duplicates", "text": "我首页消息路径图也做完了"}
    )

    assert result["results"][0]["archived_duplicate_ids"]
    active = repository.list_memories("task-duplicates", memory_type="task", status="active", limit=10)
    assert len(active) == 1
    assert active[0].task_status == "done"


def test_manual_task_correction_updates_structured_status() -> None:
    task = repository.insert_memory(
        "task-correct",
        MemoryCandidate("task", "正在制作报告", 0.8, 0.9, task_status="in_progress", subject="用户", predicate="报告", object_value="报告"),
        source_note_id="task-source",
    )
    corrected = repository.correct_memory(task.id, "这个报告已经做完了")

    assert corrected is not None
    assert corrected.task_status == "done"
    assert corrected.current_version == 2


def test_edit_pending_task_revalidates_and_applies_status(monkeypatch) -> None:
    _enable_task_identity(monkeypatch)
    target = repository.insert_memory(
        "task-pending-edit",
        MemoryCandidate("task", "正在整理报告", 0.8, 0.9, task_status="in_progress", subject="用户", predicate="报告", object_value="报告"),
        source_note_id="task-target",
    )
    candidate = MemoryCandidate(
        "task", "正在整理报告", 0.8, 0.9, task_status="in_progress", subject="用户", predicate="报告", object_value="报告",
        note_id="task-pending-note", space_id="task-pending-edit",
    )
    decision = MemoryDecision(
        candidate_id=candidate.candidate_id,
        relation="update_task",
        target_memory_ids=[target.id],
        confidence=0.7,
        reason="manual_review_test",
        evidence=["note:task-pending-note"],
        recommended_action="pending_review",
    )
    pending_id = repository.apply_memory_decision(
        "task-pending-edit", "task-pending-note", candidate, decision,
    )["memory_id"]

    approved = repository.edit_pending_memory(pending_id, "报告已经做完了")

    assert approved is not None
    assert approved.id == target.id
    assert approved.task_status == "done"
