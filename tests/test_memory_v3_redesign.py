from __future__ import annotations

import uuid

from memory import extractor
from memory.adjudicator import adjudicate_memory
from memory.canonicalizer import canonicalize_candidate
from memory.models import MEMORY_KEY_V3_VERSION, MemoryCandidate
from memory.repository import insert_memory, list_memories
from memory.service import process_note_memory


def _enable_v3(monkeypatch) -> None:
    """验证“启用v3”场景的预期行为与回归边界。"""
    from core import settings

    monkeypatch.setattr(settings, "MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_CANONICAL_KEY_V3_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_RELATION_GUARD_V3_ENABLED", True)
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "rules")


def test_v3_task_lifecycle_keeps_one_memory_and_versions(monkeypatch):
    """验证“v3任务生命周期keepsone记忆andversions”场景的预期行为与回归边界。"""
    _enable_v3(monkeypatch)
    notes = [
        "记得给随心记的大模型换一个供应商",
        "正在给随心记的大模型换 DeepSeek 供应商",
        "随心记的大模型供应商已经从 OpenAI 换成 DeepSeek 了",
    ]
    for index, text in enumerate(notes, start=1):
        process_note_memory({"id": f"lifecycle-{index}", "space_id": "v3-space", "text": text})

    memories = list_memories("v3-space", memory_type="task")

    assert len(memories) == 1
    memory = memories[0]
    assert memory.memory_key == "task:随心记:大模型供应商:更换:global"
    assert memory.memory_key_version == MEMORY_KEY_V3_VERSION
    assert memory.task_status == "done"
    assert memory.scope["new_value"] == "DeepSeek"
    assert {source.note_id for source in memory.sources} == {"lifecycle-1", "lifecycle-2", "lifecycle-3"}
    assert memory.current_version == 3


def test_v3_task_keys_prevent_template_based_false_merge(monkeypatch):
    """验证“v3任务键列表preventtemplatebasedfalse合并”场景的预期行为与回归边界。"""
    _enable_v3(monkeypatch)
    process_note_memory({"id": "task-a", "space_id": "v3-space", "text": "记得给随心记的大模型换一个供应商"})
    process_note_memory({"id": "task-b", "space_id": "v3-space", "text": "记得制作首页消息路径图"})

    memories = list_memories("v3-space", memory_type="task")

    assert len(memories) == 2
    assert len({memory.memory_key for memory in memories}) == 2


def test_v3_generic_semantic_facts_do_not_auto_merge(monkeypatch):
    """验证“v3genericsemanticfactsdonotauto合并”场景的预期行为与回归边界。"""
    _enable_v3(monkeypatch)
    old = canonicalize_candidate(
        MemoryCandidate(
            "semantic",
            "用户周末会参加摄影活动",
            0.8,
            0.9,
            subject="用户",
            predicate="fact",
            object_value="周末摄影活动",
        )
    )
    stored = insert_memory("v3-space", old, source_note_id="semantic-a")
    candidate = canonicalize_candidate(
        MemoryCandidate(
            "semantic",
            "用户养了一只叫团子的猫",
            0.8,
            0.9,
            subject="用户",
            predicate="fact",
            object_value="养猫",
        )
    )

    decision = adjudicate_memory(candidate, [stored])

    assert decision.recommended_action == "insert"
    assert decision.target_memory_ids == []


def test_hybrid_uses_model_candidates_without_union(monkeypatch):
    """验证“混合uses模型candidateswithoutunion”场景的预期行为与回归边界。"""
    from core import settings

    _enable_v3(monkeypatch)
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **_: {
            "candidates": [
                {
                    "memory_type": "task",
                    "entity": "随心记",
                    "attribute": "README",
                    "operation": "完善",
                    "canonical_topic": "完善随心记README",
                    "task_status": "todo",
                    "old_value": None,
                    "new_value": None,
                    "content": "完善随心记 README",
                    "evidence_span": "记得完善随心记 README",
                    "valid_from": None,
                    "valid_until": None,
                    "confidence": 0.91,
                    "importance": 0.88,
                    "should_store": True,
                    "extraction_reason": "明确待办",
                    "entities": ["README"],
                }
            ]
        },
    )

    candidates = extractor.extract_candidates("hybrid-1", "我喜欢乌龙茶，记得完善随心记 README")

    assert settings.MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED is True
    assert [candidate.memory_type for candidate in candidates] == ["task"]
    assert candidates[0].memory_key_version == MEMORY_KEY_V3_VERSION


def test_learning_task_lifecycle_is_stable_when_model_labels_vary_or_is_empty(monkeypatch):
    """验证“learning任务生命周期是否为stablewhen模型labelsvaryor是否为empty”场景的预期行为与回归边界。"""
    _enable_v3(monkeypatch)
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")
    model_outputs = iter(
        [
            {
                "candidates": [
                    {
                        "memory_type": "task",
                        "entity": "用户",
                        "attribute": "RAG知识",
                        "operation": "学习",
                        "canonical_topic": "学习RAG知识",
                        "task_status": "todo",
                        "old_value": None,
                        "new_value": None,
                        "content": "去学习RAG知识",
                        "evidence_span": "去学习rag知识",
                        "valid_from": None,
                        "valid_until": None,
                        "confidence": 0.9,
                        "importance": 0.8,
                        "should_store": True,
                        "extraction_reason": "明确待办",
                        "entities": ["RAG"],
                    }
                ]
            },
            {
                "candidates": [
                    {
                        "memory_type": "task",
                        "entity": "用户",
                        "attribute": "当前学习主题",
                        "operation": "学习",
                        "canonical_topic": "学习RAG知识",
                        "task_status": "in_progress",
                        "old_value": None,
                        "new_value": None,
                        "content": "正在学习RAG知识",
                        "evidence_span": "正在学rag知识呢",
                        "valid_from": None,
                        "valid_until": None,
                        "confidence": 0.9,
                        "importance": 0.8,
                        "should_store": True,
                        "extraction_reason": "明确进行中任务",
                        "entities": ["RAG"],
                    }
                ]
            },
            {"candidates": []},
        ]
    )
    monkeypatch.setattr(extractor, "complete_json", lambda **_: next(model_outputs))

    for index, text in enumerate(
        ("记得去学习rag知识啊", "正在学rag知识呢，有点多", "哦豁，成功学完rag知识。我很厉害啊"),
        start=1,
    ):
        process_note_memory({"id": f"learning-{index}", "space_id": "learning-space", "text": text})

    memories = list_memories("learning-space", memory_type="task")

    assert len(memories) == 1
    memory = memories[0]
    assert memory.memory_key == "task:用户:rag知识:学习:global"
    assert memory.task_status == "done"
    assert memory.current_version == 3
    assert {source.note_id for source in memory.sources} == {"learning-1", "learning-2", "learning-3"}


def test_completion_wording_ignores_drifting_model_slots_for_one_lifecycle(monkeypatch):
    """验证“completionwordingignoresdrifting模型slotsforone生命周期”场景的预期行为与回归边界。"""
    _enable_v3(monkeypatch)
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")
    model_outputs = iter(
        [
            {"candidates": [{"memory_type": "task", "entity": "记忆验收Zeta测试报告", "attribute": "完成状态", "operation": "完成", "canonical_topic": "完成记忆验收Zeta测试报告", "task_status": "todo", "old_value": None, "new_value": None, "content": "需要完成记忆验收 Zeta 的测试报告", "evidence_span": "我需要完成记忆验收 Zeta 的测试报告", "valid_from": None, "valid_until": None, "confidence": 0.9, "importance": 0.8, "should_store": True, "extraction_reason": "待办", "entities": ["Zeta"]}]},
            {"candidates": [{"memory_type": "task", "entity": "记忆验收Zeta的测试报告", "attribute": "记忆验收Zeta的测试报告", "operation": "完成", "canonical_topic": "完成记忆验收Zeta的测试报告", "task_status": "in_progress", "old_value": None, "new_value": None, "content": "正在完成记忆验收 Zeta 的测试报告", "evidence_span": "我正在完成记忆验收 Zeta 的测试报告", "valid_from": None, "valid_until": None, "confidence": 0.9, "importance": 0.8, "should_store": True, "extraction_reason": "进行中", "entities": ["Zeta"]}]},
            {"candidates": [{"memory_type": "task", "entity": "Zeta的测试报告", "attribute": "记忆验收Zeta的测试报告", "operation": "完成", "canonical_topic": "完成记忆验收Zeta测试报告", "task_status": "done", "old_value": None, "new_value": None, "content": "完成记忆验收 Zeta 的测试报告", "evidence_span": "我已经完成记忆验收 Zeta 的测试报告", "valid_from": None, "valid_until": None, "confidence": 0.95, "importance": 0.8, "should_store": True, "extraction_reason": "完成", "entities": ["Zeta"]}]},
        ]
    )
    monkeypatch.setattr(extractor, "complete_json", lambda **_: next(model_outputs))

    for index, text in enumerate(
        ("我需要完成记忆验收 Zeta 的测试报告", "我正在完成记忆验收 Zeta 的测试报告", "我已经完成记忆验收 Zeta 的测试报告"),
        start=1,
    ):
        process_note_memory({"id": f"completion-{index}", "space_id": "completion-space", "text": text})

    memories = list_memories("completion-space", memory_type="task")

    assert len(memories) == 1
    assert memories[0].memory_key == "task:用户:记忆验收zeta的测试报告:执行:global"
    assert memories[0].task_status == "done"
    assert memories[0].current_version == 3


def test_specific_task_name_refines_an_existing_short_name(monkeypatch):
    """验证“specific任务名称refinesanexistingshort名称”场景的预期行为与回归边界。"""
    _enable_v3(monkeypatch)

    for index, text in enumerate(
        ("我需要做简历", "我正在做简历", "我已经做完agent开发的简历了"),
        start=1,
    ):
        process_note_memory({"id": f"resume-{index}", "space_id": "resume-space", "text": text})

    memories = list_memories("resume-space", memory_type="task")

    assert len(memories) == 1
    assert memories[0].memory_key == "task:用户:agent开发的简历:制作:global"
    assert memories[0].task_status == "done"
    assert memories[0].current_version == 3


def test_implicit_reopen_after_done_stays_pending_review(monkeypatch):
    """A bare “正在做” must not silently reopen a completed task."""
    _enable_v3(monkeypatch)
    space_id = f"conflict-space-{uuid.uuid4().hex}"

    process_note_memory({"id": "conflict-1", "space_id": space_id, "text": "我需要完成记忆验收的冲突"})
    process_note_memory({"id": "conflict-2", "space_id": space_id, "text": "我已经完成记忆验收的冲突"})
    report = process_note_memory({"id": "conflict-3", "space_id": space_id, "text": "我正在完成记忆验收的冲突"})

    active = list_memories(space_id, status="active", memory_type="task")
    pending = list_memories(space_id, status="pending_review", memory_type="task")

    assert report["results"][0]["action"] == "pending_review"
    assert len(active) == 1
    assert active[0].task_status == "done"
    assert active[0].current_version == 2
    assert len(pending) == 1
    assert pending[0].task_status == "in_progress"
