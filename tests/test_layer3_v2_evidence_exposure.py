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

def test_selected_evidence_is_scored_separately_from_raw_retrieval():
    pred = {
        "case_id": "evidence_case",
        "dataset": "current_state_retrieval",
        "answer_result": {"answer_type": "answered", "evidence_bundle": {"items": []}},
        "selected_context_refs": ["m1", "m2"],
        "selected_context_ref_status": "available",
        "answer_selected_memory_refs": ["m1", "m2"],
        "answer_selected_version_refs": [],
        "retrieved_refs": ["m2", "m1"],
        "answer": "fact",
        "answer_source_citations": [],
        "access_context": {},
        "pre_snapshot": {},
        "post_snapshot": {},
        "memory_snapshot_input": {
            "memories": [
                {"memory_ref": "m1", "status": "active", "sensitivity": "normal", "content": "target fact"},
                {"memory_ref": "m2", "status": "active", "sensitivity": "normal", "content": "irrelevant fact"},
            ],
            "versions": [],
            "sources": [],
        },
        "expected": {
            "answer_type": "answered",
            "relevant_current_refs": ["m1"],
            "relevant_history_refs": [],
            "must_not_return_refs": [],
            "graded_relevance": {"m1": 3},
            "expected_claims": [],
            "required_citation_refs": [],
            "evidence_mode": "current",
        },
    }
    scored = score_case(pred)
    assert scored["retrieval"]["rank"]["1"]["hit"] is False
    assert scored["selected_evidence"] == {
        "available": True,
        "eligible": True,
        "refs": ["m1", "m2"],
        "tp": 1,
        "fp": 1,
        "fn": 0,
        "precision": 0.5,
        "recall": 1.0,
        "f1": 0.666667,
    }
