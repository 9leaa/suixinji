from __future__ import annotations

from agent import query_agent
from agent.answer_models import EvidenceBundle, RetrievalEvidence


def test_topic_gate_rejects_same_type_different_preference_attribute(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED", True)
    evidence = query_agent._relevant_evidence_items(
        "我最喜欢的电影是什么？",
        [{"id": "coffee", "memory_type": "preference", "content": "用户喜欢咖啡", "predicate": "对咖啡的偏好", "score": 0.98}],
    )

    assert evidence == []


def test_topic_gate_keeps_matching_preference_attribute(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED", True)
    evidence = query_agent._relevant_evidence_items(
        "我喜欢咖啡吗？",
        [{"id": "coffee", "memory_type": "preference", "content": "用户喜欢咖啡", "predicate": "对咖啡的偏好", "score": 0.98}],
    )

    assert [item["id"] for item in evidence] == ["coffee"]


def test_topic_gate_uses_canonical_topic_when_content_is_short(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED", True)
    item = {"id": "movie", "memory_type": "preference", "content": "喜欢", "scope": {"canonical_topic": "最喜欢的电影"}, "score": 0.98}

    assert query_agent._relevant_evidence_items("我最喜欢的电影是什么？", [item]) == [item]


def test_runtime_evidence_backfill_uses_same_topic_gate(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED", True)
    monkeypatch.setattr(query_agent, "_memory_search_compat", lambda *args, **kwargs: [])
    monkeypatch.setattr(query_agent, "_mixed_language_query_rewrites", lambda question: [])
    monkeypatch.setattr(query_agent, "_stale_history_fallback", lambda *args, **kwargs: [])

    bundle = EvidenceBundle(
        items=[
            RetrievalEvidence(
                kind="memory",
                id="coffee",
                memory_id="coffee",
                memory_type="preference",
                score=0.98,
                selected=True,
                role="current",
                metadata={"content": "用户喜欢咖啡"},
            )
        ]
    )

    class RuntimeContext:
        metadata = {"answer_evidence_bundle": bundle.to_dict()}

    monkeypatch.setattr(
        query_agent,
        "_run_answer_question_with_context",
        lambda *args, **kwargs: ("用户喜欢咖啡。", RuntimeContext()),
    )

    result = query_agent.answer_question_result("space", "我最喜欢的电影是什么？")

    assert result.answer_type == "no_answer"
    assert result.reason_code == "no_relevant_evidence"
    assert result.selected_context_refs == []


def test_ambiguous_reference_candidates_survive_for_clarification(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED", True)
    evidence = query_agent._relevant_evidence_items(
        "那个评测现在怎么样了？",
        [
            {"id": "m1", "memory_type": "task", "content": "完善第一阶段评测", "score": 0.59},
            {"id": "m2", "memory_type": "task", "content": "完善第二阶段评测", "score": 0.59},
        ],
    )

    assert {item["id"] for item in evidence} == {"m1", "m2"}
