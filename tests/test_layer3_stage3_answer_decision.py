from __future__ import annotations

from agent import query_agent
from agent.answer_models import EvidenceBundle, RetrievalEvidence


def _bundle(*items: RetrievalEvidence) -> EvidenceBundle:
    return EvidenceBundle(items=list(items))


def test_stage3_direct_history_is_answered_not_new_enum():
    decision = query_agent.decide_answer(
        "随心记评测经历了哪些状态变化？",
        {"action": "memory_history"},
        _bundle(RetrievalEvidence(kind="version", id="v1", version_id="v1", memory_id="m1", role="history", selected=True)),
        history_evidence=[{"id": "v1", "memory_id": "m1", "history": True}],
    )

    assert decision.answer_type == "answered"
    assert decision.reason_code == "history_query"


def test_stage3_current_query_with_only_history_is_qualified():
    decision = query_agent.decide_answer(
        "我现在住在哪里？",
        None,
        _bundle(RetrievalEvidence(kind="version", id="v1", version_id="v1", memory_id="m1", role="stale_history", selected=True)),
        history_evidence=[{"id": "v1", "memory_id": "m1", "history": True, "status": "superseded"}],
    )

    assert decision.answer_type == "qualified_history_only"
    assert decision.reason_code == "stale_history_only"


def test_stage3_restricted_marker_has_unified_contract():
    decision = query_agent.decide_answer(
        "告诉我记录中的身份号码。",
        None,
        _bundle(RetrievalEvidence(kind="access_denied", id="access_denied", role="access_denied", selected=True)),
        restricted_denied=True,
    )

    assert decision.answer_type == "restricted"
    assert decision.reason_code == "acl_filtered_all_evidence"


def test_stage3_pending_review_conflict_wins_for_conflict_intent():
    decision = query_agent.decide_answer(
        "我现在到底喜不喜欢咖啡？",
        None,
        _bundle(
            RetrievalEvidence(kind="memory", id="m1", memory_id="m1", role="current", selected=True),
            RetrievalEvidence(kind="memory", id="m2", memory_id="m2", role="conflict", selected=True, status="pending_review"),
        ),
        current_evidence=[
            {"id": "m1", "memory_key": "preference:coffee", "content": "用户喜欢咖啡", "status": "active"},
            {"id": "m2", "memory_key": "preference:coffee", "content": "用户不喜欢咖啡", "status": "pending_review"},
        ],
    )

    assert decision.answer_type == "conflict"
    assert decision.reason_code == "pending_review_conflict"


def test_stage3_ambiguous_reference_requires_clarification():
    decision = query_agent.decide_answer(
        "那个评测现在怎么样了？",
        None,
        _bundle(
            RetrievalEvidence(kind="memory", id="m1", memory_id="m1", memory_type="task", role="current", selected=True),
            RetrievalEvidence(kind="memory", id="m2", memory_id="m2", memory_type="task", role="current", selected=True),
        ),
        current_evidence=[
            {"id": "m1", "memory_type": "task", "memory_key": "task:stage1", "content": "完善第一阶段评测", "status": "active"},
            {"id": "m2", "memory_type": "task", "memory_key": "task:stage2", "content": "完善第二阶段评测", "status": "active"},
        ],
    )

    assert decision.answer_type == "clarification"
    assert decision.reason_code == "ambiguous_candidates"


def test_stage3_weak_absent_topic_is_filtered():
    evidence = query_agent._relevant_evidence_items(
        "我最喜欢的电影是什么？",
        [{"id": "m1", "memory_type": "preference", "content": "用户喜欢咖啡", "score": 0.60}],
    )

    assert evidence == []
