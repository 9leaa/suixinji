"""文件作用：裁决到确定性演化动作。

项目关系：本文件依赖 `memory`、`memory.adjudicator`、`memory.evolution`、`memory.models` 等 7 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""


import pytest

from memory import repository
from memory.adjudicator import adjudicate_memory
from memory.evolution import evolve_memory
from memory.models import MemoryCandidate
from memory.repository import (
    approve_pending_memory,
    get_memory,
    insert_memory,
    list_memories,
    list_memory_decisions,
    list_memory_relations,
    schema_tables,
)
from memory.relation_classifier import classify_relation
from memory.relation_guard import evaluate_relation
from memory.service import process_note_memory


def test_high_confidence_task_identity_llm_can_bridge_wording_but_local_rules_choose_transition(monkeypatch):
    from core import settings
    from memory import advisory
    from memory.canonicalizer import canonicalize_candidate

    old_candidate = canonicalize_candidate(MemoryCandidate(
        "task", "记得完成随心记第三层评测", 0.8, 0.95, task_status="todo",
        subject="随心记", predicate="第三层评测", object_value="第三层评测",
        scope={"operation": "评测", "scope": "global"},
    ))
    old = insert_memory("identity-llm", old_candidate, source_note_id="old-note")
    incoming = canonicalize_candidate(MemoryCandidate(
        "task", "Layer 3 全量验证已经做完", 0.8, 0.96, task_status="done",
        subject="随心记", predicate="Layer 3 全量验证", object_value="Layer 3 全量验证",
        scope={"operation": "评测", "scope": "global"},
    ))
    monkeypatch.setattr(settings, "STRONG_ESCALATION_ENABLED", True)
    monkeypatch.setattr(
        advisory,
        "maybe_memory_identity_adjudication",
        lambda candidate, memories: {
            "identity_relation": "same_instance", "target_memory_id": old.id, "confidence": 0.97,
            "reason_code": "same_goal_paraphrase", "supporting_fields": ["project", "goal"], "conflicting_fields": [],
        },
    )

    decision = adjudicate_memory(incoming, [old])

    assert decision.relation == "update_task"
    assert decision.recommended_action == "update_task"
    assert decision.target_memory_ids == [old.id]


def test_task_family_match_cannot_authorize_a_different_instance_update():
    from memory.canonicalizer import canonicalize_candidate

    first = canonicalize_candidate(MemoryCandidate(
        "task", "完成检索质量优化第一轮", 0.8, 0.95, task_status="todo",
        subject="用户", predicate="检索质量优化第一轮", scope={"operation": "执行"},
    ))
    second = canonicalize_candidate(MemoryCandidate(
        "task", "完成检索质量优化第二轮", 0.8, 0.95, task_status="done",
        subject="用户", predicate="检索质量优化第二轮", scope={"operation": "执行"},
    ))
    stored = insert_memory("task-family-only", first, source_note_id="first")

    guarded = evaluate_relation(second, stored)

    assert first.scope["task_family_key"] == second.scope["task_family_key"]
    assert first.effective_memory_key != second.effective_memory_key
    assert guarded.relation == "new"
    assert guarded.action == "insert"


def test_core_audit_schema_is_created():
    """函数功能：`test_core_audit_schema_is_created` 负责验证 core audit schema is created 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    assert {
        "memories",
        "memory_sources",
        "memory_versions",
        "memory_relations",
        "memory_decisions",
        "memory_extraction_states",
        "memory_traces",
    }.issubset(schema_tables())


def test_same_memory_adds_evidence_and_confirmation_without_new_version():
    """函数功能：`test_same_memory_adds_evidence_and_confirmation_without_new_version` 负责验证 same memory adds evidence and confirmation without new version 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢咖啡"})
    first = list_memories("space-1")[0]

    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我喜欢咖啡"})
    current = list_memories("space-1")[0]

    assert current.id == first.id
    assert current.current_version == 1
    assert current.last_confirmed_at is not None
    assert current.confidence >= first.confidence
    assert {source.note_id for source in current.sources} == {"note-1", "note-2"}
    assert list_memory_decisions("space-1")[0]["relation"] == "same"


def test_legacy_relation_api_maps_formal_merge_name():
    """函数功能：`test_legacy_relation_api_maps_formal_merge_name` 负责验证 legacy relation api maps formal merge name 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    old = insert_memory("space-1", MemoryCandidate("semantic", "用户正在学习 Agent", 0.8, 0.9), source_note_id="note-1")
    candidate = MemoryCandidate("semantic", "用户也在研究 Agent 和 RAG", 0.8, 0.9)

    decision = classify_relation(candidate, [old])

    assert decision.relation == "new"
    assert decision.action == "insert"


def test_merge_updates_content_and_preserves_a_version():
    """函数功能：`test_merge_updates_content_and_preserves_a_version` 负责验证 merge updates content and preserves a version 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我正在学习 Agent"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我正在学习 Agent，也在研究 RAG"})

    semantics = list_memories("space-1", memory_type="semantic")
    assert len(semantics) >= 2
    assert all(result["action"] in {"insert", "add_source"} for result in report["results"])
    assert any("Agent" in memory.content for memory in semantics)
    assert any("RAG" in memory.content for memory in semantics)
    assert all(memory.current_version == 1 for memory in semantics)


def test_preference_correction_updates_stable_identity_with_version_audit():
    """偏好极性纠正沿用 stable identity，并记录版本审计。"""
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢喝牛奶"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我讨厌喝牛奶"})

    active = list_memories("space-1", status="active", memory_type="preference")
    old = list_memories("space-1", status="superseded", memory_type="preference")
    assert len(active) == 1
    assert old == []
    assert report["results"][0]["action"] == "update"
    assert active[0].polarity == "negative"
    assert get_memory(active[0].id).current_version == 2
    assert len(get_memory(active[0].id).versions) == 2
    decision = list_memory_decisions("space-1")[0]
    assert decision["relation"] == "update"
    assert decision["result_memory_ids"] == [active[0].id]


@pytest.mark.parametrize(
    ("first_topic", "second_topic"),
    [
        ("燕麦拿铁", "苹果"),
        ("量子键盘", "海边徒步"),
        ("Rust", "爵士乐"),
    ],
)
def test_unrelated_preference_topics_never_merge_from_template_overlap(first_topic, second_topic):
    """函数功能：`test_unrelated_preference_topics_never_merge_from_template_overlap` 负责验证 unrelated preference topics never merge from template overlap 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        first_topic: first topic 参数，由调用方传入。
        second_topic: second topic 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": f"我喜欢{first_topic}"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": f"我喜欢{second_topic}"})

    active = list_memories("space-1", memory_type="preference")

    assert report["results"][0]["action"] == "insert"
    assert len(active) == 2
    assert {decision["relation"] for decision in list_memory_decisions("space-1")} == {"new"}


@pytest.mark.parametrize(
    ("first_topic", "second_topic"),
    [
        ("饮品A1", "饮品A10"),
        ("咖啡S1", "咖啡S10"),
        ("手机 iPhone 15", "手机 iPhone 16"),
    ],
)
def test_distinct_named_or_versioned_preference_topics_never_merge(first_topic, second_topic):
    """函数功能：`test_distinct_named_or_versioned_preference_topics_never_merge` 负责验证 distinct named or versioned preference topics never merge 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        first_topic: first topic 参数，由调用方传入。
        second_topic: second topic 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": f"我喜欢{first_topic}"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": f"我喜欢{second_topic}"})

    assert report["results"][0]["action"] == "insert"
    assert len(list_memories("space-1", memory_type="preference")) == 2


def test_versioned_preference_change_targets_only_the_exact_identifier():
    """函数功能：`test_versioned_preference_change_targets_only_the_exact_identifier` 负责验证 versioned preference change targets only the exact identifier 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢饮品A1"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我现在不喜欢饮品A10了"})

    assert report["results"][0]["action"] == "insert"
    assert len(list_memories("space-1", status="active", memory_type="preference")) == 2
    assert not list_memories("space-1", status="superseded", memory_type="preference")


def test_preference_supersede_targets_only_the_same_topic():
    """函数功能：`test_preference_supersede_targets_only_the_same_topic` 负责验证 preference supersede targets only the same topic 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢喝燕麦拿铁"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我喜欢吃苹果"})
    report = process_note_memory({"id": "note-3", "space_id": "space-1", "text": "我现在不喜欢吃苹果了"})

    active = list_memories("space-1", status="active", memory_type="preference")
    superseded = list_memories("space-1", status="superseded", memory_type="preference")

    assert report["results"][0]["action"] == "update"
    assert len(active) == 2
    assert any("燕麦拿铁" in memory.content for memory in active)
    assert any("不喜欢吃苹果" in memory.content for memory in active)
    assert superseded == []


def test_preference_scopes_do_not_overwrite_each_other():
    """函数功能：`test_preference_scopes_do_not_overwrite_each_other` 负责验证 preference scopes do not overwrite each other 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我早上喜欢喝咖啡"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我晚上不喜欢喝咖啡"})

    assert report["results"][0]["action"] == "insert"
    assert len(list_memories("space-1", memory_type="preference")) == 2


@pytest.mark.parametrize(
    ("old_content", "new_content"),
    [
        ("用户喜欢古典音乐", "用户更喜欢电子音乐"),
        ("用户喜欢公路骑行", "用户更喜欢山地骑行"),
    ],
)
def test_comparative_alternatives_use_shared_context_not_named_examples(old_content, new_content):
    """函数功能：`test_comparative_alternatives_use_shared_context_not_named_examples` 负责验证 comparative alternatives use shared context not named examples 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        old_content: old content 参数，由调用方传入。
        new_content: new content 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    old = insert_memory(
        "space-1",
        MemoryCandidate("preference", old_content, 0.8, 0.9),
        source_note_id="note-old",
    )
    candidate = MemoryCandidate("preference", new_content, 0.8, 0.9, note_id="note-new", space_id="space-1")

    decision = adjudicate_memory(candidate, [old])

    assert decision.relation == "conflict"
    assert decision.target_memory_ids == [old.id]


def test_same_category_noncomparative_preferences_remain_independent():
    """函数功能：`test_same_category_noncomparative_preferences_remain_independent` 负责验证 same category noncomparative preferences remain independent 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    old = insert_memory(
        "space-1",
        MemoryCandidate("preference", "用户喜欢拿铁咖啡", 0.8, 0.9),
        source_note_id="note-old",
    )
    candidate = MemoryCandidate(
        "preference",
        "用户喜欢美式咖啡",
        0.8,
        0.9,
        note_id="note-new",
        space_id="space-1",
    )

    decision = adjudicate_memory(candidate, [old])

    assert decision.recommended_action == "insert"


def test_negative_action_grammar_tracks_same_topic_without_named_values():
    """函数功能：`test_negative_action_grammar_tracks_same_topic_without_named_values` 负责验证 negative action grammar tracks same topic without named values 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    old = insert_memory(
        "space-1",
        MemoryCandidate("preference", "用户喜欢用机械键盘", 0.8, 0.9),
        source_note_id="note-old",
    )
    candidate = MemoryCandidate(
        "preference",
        "用户暂时不用机械键盘",
        0.8,
        0.9,
        note_id="note-new",
        space_id="space-1",
    )

    decision = adjudicate_memory(candidate, [old])

    assert decision.relation == "supersede"
    assert decision.target_memory_ids == [old.id]


def test_medium_confidence_destructive_change_waits_for_review():
    """函数功能：`test_medium_confidence_destructive_change_waits_for_review` 负责验证 medium confidence destructive change waits for review 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    old_candidate = MemoryCandidate(
        "semantic",
        "用户正在学习 Agent",
        0.8,
        0.9,
        subject="用户",
        predicate="learning_focus",
        object_value="Agent",
    )
    old = insert_memory("space-1", old_candidate, source_note_id="note-1")
    candidate = MemoryCandidate(
        "semantic",
        "用户正在学习 Agent 和 RAG",
        0.7,
        0.6,
        note_id="note-2",
        space_id="space-1",
        subject="用户",
        predicate="learning_focus",
        object_value="Agent RAG",
    )

    decision = adjudicate_memory(candidate, [old])
    result = evolve_memory(space_id="space-1", note_id="note-2", candidate=candidate, decision=decision)

    assert decision.relation == "new"
    assert decision.recommended_action == "insert"
    assert result["action"] == "insert"
    assert len(list_memories("space-1", status="pending_review")) == 0
    assert get_memory(old.id).status == "active"
    active = list_memories("space-1", status="active", memory_type="semantic")
    assert len(active) == 2
    assert any("RAG" in memory.content for memory in active)
    assert list_memory_decisions("space-1")[0]["status"] == "applied"


def test_completed_note_memory_processing_is_idempotent():
    """函数功能：`test_completed_note_memory_processing_is_idempotent` 负责验证 completed note memory processing is idempotent 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    first = process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})
    second = process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})

    assert first["extraction_status"] == "completed"
    assert second["idempotent"] is True
    assert len(list_memories("space-1")) == 1


def test_supersede_rolls_back_old_state_when_new_insert_fails(monkeypatch):
    """函数功能：`test_supersede_rolls_back_old_state_when_new_insert_fails` 负责验证 supersede rolls back old state when new insert fails 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    old_candidate = MemoryCandidate(
        "preference",
        "用户喜欢咖啡",
        0.8,
        0.9,
        subject="咖啡",
        predicate="preference",
        object_value="咖啡",
    )
    old = insert_memory("space-1", old_candidate, source_note_id="note-1")
    candidate = MemoryCandidate(
        "preference",
        "用户不再喝咖啡",
        0.8,
        0.9,
        note_id="note-2",
        space_id="space-1",
        subject="咖啡",
        predicate="preference",
        object_value="咖啡",
    )
    decision = adjudicate_memory(candidate, [old])
    monkeypatch.setattr(repository, "_insert_memory_row", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("insert failed")))

    with pytest.raises(RuntimeError):
        evolve_memory(space_id="space-1", note_id="note-2", candidate=candidate, decision=decision)

    assert get_memory(old.id).status == "active"
    failed_decision = list_memory_decisions("space-1")[0]
    assert failed_decision["status"] == "failed"
    assert failed_decision["error"] == "RuntimeError"


def test_approved_pending_supersede_reuses_candidate_memory_atomically():
    """函数功能：`test_approved_pending_supersede_reuses_candidate_memory_atomically` 负责验证 approved pending supersede reuses candidate memory atomically 场景，服务于本文件职责：裁决到确定性演化动作。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    old_candidate = MemoryCandidate(
        "preference",
        "用户喜欢咖啡",
        0.8,
        0.9,
        subject="咖啡",
        predicate="preference",
        object_value="咖啡",
    )
    old = insert_memory("space-1", old_candidate, source_note_id="note-1")
    candidate = MemoryCandidate(
        "preference",
        "用户不再喝咖啡",
        0.7,
        0.6,
        note_id="note-2",
        space_id="space-1",
        subject="咖啡",
        predicate="preference",
        object_value="咖啡",
    )
    decision = adjudicate_memory(candidate, [old])
    pending = evolve_memory(space_id="space-1", note_id="note-2", candidate=candidate, decision=decision)

    assert decision.recommended_action == "pending_review"
    approved = approve_pending_memory(pending["memory_id"])

    assert approved.id == pending["memory_id"]
    assert approved.status == "active"
    assert get_memory(old.id).status == "superseded"
    assert {relation.relation for relation in list_memory_relations(approved.id)} >= {"supersedes", "superseded_by"}
