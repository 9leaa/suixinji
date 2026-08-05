"""文件作用：Inbox/Task 状态对账。

项目关系：本文件依赖 `core`、`memory`、`memory.models`、`memory.service`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

import pytest

from memory import extractor
from memory import repository
from memory.models import MemoryCandidate, MemoryDecision
from memory.service import format_memory_profile, process_note_memory
from memory.task_state import infer_task_status, validate_task_status


def _enable_task_identity(monkeypatch) -> None:
    """函数功能：`_enable_task_identity` 负责处理 enable task identity，服务于本文件职责：Inbox/Task 状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from core import settings

    monkeypatch.setattr(settings, "MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_CANONICAL_KEY_V3_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_RELATION_GUARD_V3_ENABLED", True)
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "rules")


def test_all_unfinished_wording_is_todo_and_legacy_input_is_normalized() -> None:
    assert infer_task_status("我正在整理报告，继续处理") == "todo"
    assert infer_task_status("任务卡住了，正在等待权限") == "todo"
    assert validate_task_status("in_progress") == "todo"
    assert validate_task_status("blocked") == "todo"
    assert validate_task_status("cancelled") == "done"
    with pytest.raises(ValueError, match="invalid task_status"):
        MemoryCandidate("task", "正在整理报告", 0.8, 0.9, task_status="in_progress")


@pytest.mark.parametrize(("legacy", "expected"), [("in_progress", "todo"), ("blocked", "todo"), ("cancelled", "done")])
def test_legacy_task_rows_are_read_as_two_states_without_rewriting_data(legacy: str, expected: str) -> None:
    candidate = MemoryCandidate(
        "task", "整理报告", 0.8, 0.9, task_status="todo", note_id="legacy-candidate-note", space_id="legacy-status"
    )
    repository.save_memory_candidate(candidate, space_id="legacy-status")
    memory = repository.insert_memory("legacy-status", candidate, source_note_id="legacy-memory-note")

    with repository._connect() as conn:
        conn.execute("UPDATE memories SET task_status = ? WHERE id = ?", (legacy, memory.id))
        conn.execute("UPDATE memory_candidates SET task_status = ? WHERE candidate_id = ?", (legacy, candidate.candidate_id))
        conn.execute("UPDATE memory_versions SET task_status = ? WHERE memory_id = ?", (legacy, memory.id))

    assert repository.get_memory(memory.id).task_status == expected
    assert {version.task_status for version in repository.get_memory(memory.id).versions} == {expected}
    assert repository.get_memory_candidate(candidate.candidate_id).task_status == expected

    with repository._connect() as conn:
        assert conn.execute("SELECT task_status FROM memories WHERE id = ?", (memory.id,)).fetchone()[0] == legacy
        assert conn.execute("SELECT task_status FROM memory_candidates WHERE candidate_id = ?", (candidate.candidate_id,)).fetchone()[0] == legacy
        assert conn.execute("SELECT task_status FROM memory_versions WHERE memory_id = ?", (memory.id,)).fetchone()[0] == legacy


def test_task_lifecycle_merges_action_wording(monkeypatch) -> None:
    """函数功能：`test_task_lifecycle_merges_action_wording` 负责验证 task lifecycle merges action wording 场景，服务于本文件职责：Inbox/Task 状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    assert memories[0].current_version == 2


def test_legacy_generic_task_identity_is_updated(monkeypatch) -> None:
    """函数功能：`test_legacy_generic_task_identity_is_updated` 负责验证 legacy generic task identity is updated 场景，服务于本文件职责：Inbox/Task 状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`test_terminal_update_archives_duplicate_active_tasks` 负责验证 terminal update archives duplicate active tasks 场景，服务于本文件职责：Inbox/Task 状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    _enable_task_identity(monkeypatch)
    for index, text in enumerate(("制作随心记首页的消息路径图", "正在制作随心记首页消息路径图")):
        repository.insert_memory(
            "task-duplicates",
            MemoryCandidate(
                "task", text, 0.8, 0.9,
                subject="制作随心记首页消息路径图", predicate="task", object_value=text,
                task_status="todo",
            ),
            source_note_id=f"duplicate-source-{index}",
        )

    result = process_note_memory(
        {"id": "duplicate-done", "space_id": "task-duplicates", "text": "我首页消息路径图也做完了"}
    )

    # Multiple historical matches are deliberately held for review; Stage 2
    # never guesses which duplicate task should receive the completion.
    assert result["results"][0]["action"] == "pending_review"
    assert result["results"][0]["relation"] == "ambiguous_match"
    active = repository.list_memories("task-duplicates", memory_type="task", status="active", limit=10)
    assert len(active) == 2
    assert {item.task_status for item in active} == {"todo"}


def test_manual_task_correction_updates_structured_status() -> None:
    """函数功能：`test_manual_task_correction_updates_structured_status` 负责验证 manual task correction updates structured status 场景，服务于本文件职责：Inbox/Task 状态对账。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    task = repository.insert_memory(
        "task-correct",
        MemoryCandidate("task", "正在制作报告", 0.8, 0.9, task_status="todo", subject="用户", predicate="报告", object_value="报告"),
        source_note_id="task-source",
    )
    corrected = repository.correct_memory(task.id, "这个报告已经做完了")

    assert corrected is not None
    assert corrected.task_status == "done"
    assert corrected.current_version == 2


def test_edit_pending_task_revalidates_and_applies_status(monkeypatch) -> None:
    """函数功能：`test_edit_pending_task_revalidates_and_applies_status` 负责验证 edit pending task revalidates and applies status 场景，服务于本文件职责：Inbox/Task 状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    _enable_task_identity(monkeypatch)
    target = repository.insert_memory(
        "task-pending-edit",
        MemoryCandidate("task", "正在整理报告", 0.8, 0.9, task_status="todo", subject="用户", predicate="报告", object_value="报告"),
        source_note_id="task-target",
    )
    candidate = MemoryCandidate(
        "task", "正在整理报告", 0.8, 0.9, task_status="todo", subject="用户", predicate="报告", object_value="报告",
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


def test_profile_keeps_only_latest_status_for_legacy_duplicate(monkeypatch) -> None:
    _enable_task_identity(monkeypatch)
    repository.insert_memory(
        "profile-dedup",
        MemoryCandidate(
            "task", "制作随心记首页的消息路径图", 0.8, 0.9,
            subject="制作随心记首页的消息路径图", predicate="task",
            object_value="制作随心记首页的消息路径图", task_status="todo",
        ),
        source_note_id="profile-old",
    )
    repository.insert_memory(
        "profile-dedup",
        MemoryCandidate(
            "task", "用户已完成首页消息路径图的制作", 0.8, 0.9,
            subject="用户", predicate="首页消息路径图",
            object_value="首页消息路径图", task_status="done",
        ),
        source_note_id="profile-new",
    )

    profile = format_memory_profile("profile-dedup")

    assert "制作随心记首页的消息路径图" not in profile
    assert "当前任务" not in profile
