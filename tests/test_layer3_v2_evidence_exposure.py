from __future__ import annotations

from eval.layer3.run_layer3_eval import score_case


def test_empty_exposed_evidence_is_not_marked_unavailable():
    scored = score_case({
        "answer_result": {"answer_type": "no_answer", "evidence_bundle": {"items": []}},
        "selected_context_refs": [], "selected_context_ref_status": "available",
        "selected_tool_refs": [], "selected_tool_ref_status": "available",
        "executed_tools": [], "executed_tools_status": "available",
        "expected": {"expected_claims": [], "required_citation_refs": [], "no_answer": True},
        "memory_snapshot_input": {"memories": [], "versions": [], "sources": []},
        "retrieved_refs": [], "answer": "", "answer_source_citations": [], "access_context": {}, "pre_snapshot": {}, "post_snapshot": {},
    })

    assert scored["stage0_contract"] == {
        "selected_context_refs_status": "available",
        "selected_tool_refs_status": "available",
        "executed_tools_status": "available",
    }
