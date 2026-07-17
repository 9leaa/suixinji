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
from memory.service import process_note_memory


def test_core_audit_schema_is_created():
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
    old = insert_memory("space-1", MemoryCandidate("semantic", "用户正在学习 Agent", 0.8, 0.9), source_note_id="note-1")
    candidate = MemoryCandidate("semantic", "用户也在研究 Agent 和 RAG", 0.8, 0.9)

    decision = classify_relation(candidate, [old])

    assert decision.relation == "extend"
    assert decision.action == "merge"


def test_merge_updates_content_and_preserves_a_version():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我正在学习 Agent"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我正在学习 Agent，也在研究 RAG"})

    semantics = list_memories("space-1", memory_type="semantic")
    assert len(semantics) == 1
    assert report["results"][0]["action"] == "merge"
    assert "Agent" in semantics[0].content and "RAG" in semantics[0].content
    assert semantics[0].current_version == 2
    assert len(get_memory(semantics[0].id).versions) == 2


def test_supersede_is_audited_with_bidirectional_relations():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢喝牛奶"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我讨厌喝牛奶"})

    active = list_memories("space-1", status="active", memory_type="preference")
    old = list_memories("space-1", status="superseded", memory_type="preference")
    assert len(active) == len(old) == 1
    assert report["results"][0]["action"] == "supersede"
    relation_names = {relation.relation for relation in list_memory_relations(active[0].id)}
    assert {"supersedes", "superseded_by"}.issubset(relation_names)
    decision = list_memory_decisions("space-1")[0]
    assert decision["relation"] == "supersede"
    assert decision["result_memory_ids"]


@pytest.mark.parametrize(
    ("first_topic", "second_topic"),
    [
        ("燕麦拿铁", "苹果"),
        ("量子键盘", "海边徒步"),
        ("Rust", "爵士乐"),
    ],
)
def test_unrelated_preference_topics_never_merge_from_template_overlap(first_topic, second_topic):
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
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": f"我喜欢{first_topic}"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": f"我喜欢{second_topic}"})

    assert report["results"][0]["action"] == "insert"
    assert len(list_memories("space-1", memory_type="preference")) == 2


def test_versioned_preference_change_targets_only_the_exact_identifier():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢饮品A1"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我现在不喜欢饮品A10了"})

    assert report["results"][0]["action"] == "insert"
    assert len(list_memories("space-1", status="active", memory_type="preference")) == 2
    assert not list_memories("space-1", status="superseded", memory_type="preference")


def test_preference_supersede_targets_only_the_same_topic():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢喝燕麦拿铁"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我喜欢吃苹果"})
    report = process_note_memory({"id": "note-3", "space_id": "space-1", "text": "我现在不喜欢吃苹果了"})

    active = list_memories("space-1", status="active", memory_type="preference")
    superseded = list_memories("space-1", status="superseded", memory_type="preference")

    assert report["results"][0]["action"] == "supersede"
    assert len(active) == 2
    assert any("燕麦拿铁" in memory.content for memory in active)
    assert any("不喜欢吃苹果" in memory.content for memory in active)
    assert len(superseded) == 1
    assert "苹果" in superseded[0].content
    assert "燕麦拿铁" not in superseded[0].content


def test_preference_scopes_do_not_overwrite_each_other():
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

    assert decision.relation == "merge"
    assert decision.recommended_action == "pending_review"
    assert result["action"] == "pending_review"
    assert len(list_memories("space-1", status="pending_review")) == 1
    assert get_memory(old.id).status == "active"
    assert list_memory_decisions("space-1")[0]["status"] == "pending_review"

    approved = approve_pending_memory(result["memory_id"])

    assert approved.id == old.id
    assert "RAG" in approved.content
    assert get_memory(result["memory_id"]).status == "archived"
    approved_decision = list_memory_decisions("space-1")[0]
    assert approved_decision["status"] == "applied"
    assert approved_decision["recommended_action"] == "merge"


def test_completed_note_memory_processing_is_idempotent():
    first = process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})
    second = process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})

    assert first["extraction_status"] == "completed"
    assert second["idempotent"] is True
    assert len(list_memories("space-1")) == 1


def test_supersede_rolls_back_old_state_when_new_insert_fails(monkeypatch):
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
