from __future__ import annotations

from agent.answer_models import EvidenceBundle, RetrievalEvidence
from agent.query_agent import decide_answer


def test_stage6_conflict_keeps_real_pending_review_id():
    bundle = EvidenceBundle(
        items=[
            RetrievalEvidence(kind="memory", id="active-1", memory_id="active-1", selected=True, role="current", status="active"),
            RetrievalEvidence(kind="memory", id="pending-1", memory_id="pending-1", selected=True, role="conflict", status="pending_review"),
        ]
    )

    decision = decide_answer(
        "我现在到底喜不喜欢咖啡？",
        None,
        bundle,
        current_evidence=[
            {"id": "active-1", "status": "active", "memory_key": "preference:coffee", "polarity": "positive"},
            {"id": "pending-1", "status": "pending_review", "memory_key": "preference:coffee", "polarity": "negative"},
        ],
        pending_review_ids=["decision-pr1"],
    )

    assert decision.answer_type == "conflict"
    assert decision.reason_code == "pending_review_conflict"
    assert decision.conflict_ids == ["decision-pr1", "pending-1"]
