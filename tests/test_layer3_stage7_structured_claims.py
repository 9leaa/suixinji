from __future__ import annotations

from agent.answer_models import EvidenceBundle, RetrievalEvidence
from agent.query_agent import _supported_claims_from_bundle
from eval.layer3.run_layer3_eval import score_case


def test_stage7_builds_one_claim_per_selected_evidence():
    bundle = EvidenceBundle(
        items=[
            RetrievalEvidence(kind="memory", id="m1", memory_id="m1", source_ids=["s1"], selected=True, metadata={"content": "项目A:todo"}),
            RetrievalEvidence(kind="memory", id="m2", memory_id="m2", source_ids=["s2"], selected=True, metadata={"content": "项目B:blocked"}),
        ]
    )
    claims = _supported_claims_from_bundle(bundle, answer_type="answered", reason_code="evidence_supported")
    assert [claim.text for claim in claims] == ["项目A:todo", "项目B:blocked"]
    assert [claim.source_ids for claim in claims] == [["s1"], ["s2"]]


def test_stage7_evaluator_prefers_exposed_structured_claims():
    scored = score_case(
        {
            "expected": {"expected_claims": [{"claim": "项目A是todo", "memory_refs": ["m1"], "source_refs": ["s1"]}]},
            "memory_snapshot_input": {"memories": [{"memory_ref": "m1", "content": "项目A:todo"}], "versions": [], "sources": []},
            "answer": "无关的旧文本",
            "answer_structured_claims": [{"text": "项目A:todo", "memory_refs": ["m1"], "version_refs": [], "source_refs": ["s1"]}],
        }
    )
    assert scored["answer"]["claims"]["f1"] == 1.0
