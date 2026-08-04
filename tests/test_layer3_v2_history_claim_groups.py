from __future__ import annotations

from agent.answer_models import ClaimGroup, EvidenceBundle, RetrievalEvidence, SupportedClaim
from agent.query_agent import _history_fallback_answer, _supported_claims_from_bundle, _timeline_claim_groups
from eval.layer3.run_layer3_eval import score_case


def _history_bundle() -> EvidenceBundle:
    return EvidenceBundle(items=[
        RetrievalEvidence(kind="version", id="v1", memory_id="m1", version_id="v1", source_ids=["s1"], selected=True, role="history", metadata={"content": "项目待处理", "version": 1}),
        RetrievalEvidence(kind="version", id="v2", memory_id="m1", version_id="v2", source_ids=["s2"], selected=True, role="history", metadata={"content": "项目被阻塞", "version": 2}),
        RetrievalEvidence(kind="version", id="v3", memory_id="m1", version_id="v3", source_ids=["s3"], selected=True, role="history", metadata={"content": "项目已完成", "version": 3}),
    ])


def test_history_keeps_atomic_claims_and_emits_ordered_summary_group(monkeypatch):
    monkeypatch.setattr("agent.query_agent.settings.QUERY_TIMELINE_CLAIM_GROUP_ENABLED", True)
    bundle = _history_bundle()
    claims = _supported_claims_from_bundle(bundle, answer_type="answered", reason_code="history_query")
    groups = _timeline_claim_groups(bundle, claims, answer_type="answered", reason_code="history_query", answer="项目依次经历了todo、blocked、done。")

    assert len(claims) == 3
    assert [claim.claim_id for claim in claims] == ["version:v1", "version:v2", "version:v3"]
    assert len(groups) == 1
    assert groups[0].version_ids == ["v1", "v2", "v3"]
    assert groups[0].source_ids == ["s1", "s2", "s3"]
    assert groups[0].ordered_member_claim_ids == ["version:v1", "version:v2", "version:v3"]


def test_v2_scorer_scores_timeline_group_not_atomic_members():
    scored = score_case({
        "expected": {"expected_claims": [{"claim": "项目依次经历todo、blocked、done"}], "expected_claim_groups": [{"group_type": "timeline", "summary_claim": {"claim": "项目依次经历todo、blocked、done"}, "version_refs": ["v1", "v2", "v3"], "source_refs": ["s1", "s2", "s3"]}]},
        "answer_claim_groups": [{"group_type": "timeline", "summary_claim": {"text": "项目依次经历todo、blocked、done"}, "version_refs": ["v1", "v2", "v3"], "source_refs": ["s1", "s2", "s3"]}],
        "memory_snapshot_input": {"memories": [], "versions": [], "sources": []},
        "answer": "无关文本", "answer_result": {"answer_type": "answered", "reason_code": "history_query"},
        "retrieved_refs": [], "answer_source_citations": [], "access_context": {}, "pre_snapshot": {}, "post_snapshot": {},
    })

    assert scored["answer"]["claims"]["f1"] == 1.0
    assert scored["answer"]["claim_groups"]["timeline_order_accuracy"] == 1.0


def test_claim_group_rejects_non_timeline_group_type():
    summary = SupportedClaim(text="summary")
    try:
        ClaimGroup(group_type="profile", summary_claim=summary)
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("unsupported group type must fail")


def test_history_fallback_summarizes_preference_evolution():
    answer = _history_fallback_answer(
        [
            {"id": "v1", "memory_id": "m1", "version": 1, "content": "用户喜欢绿茶"},
            {"id": "v2", "memory_id": "m1", "version": 2, "content": "用户减少绿茶"},
            {"id": "v3", "memory_id": "m1", "version": 3, "content": "用户不喜欢绿茶"},
        ],
        question="我对绿茶的偏好发生过什么变化？",
    )

    assert answer == "你以前喜欢绿茶，后来偏好减弱，现在不喜欢绿茶。"


def test_history_fallback_summarizes_residence_evolution():
    answer = _history_fallback_answer(
        [
            {"id": "v1", "memory_id": "m1", "version": 1, "content": "用户居住在上海"},
            {"id": "v2", "memory_id": "m1", "version": 2, "content": "用户居住在新加坡"},
        ],
        question="我的居住地发生过什么变化？",
    )

    assert answer == "你以前住在上海，现在住在新加坡。"
