from __future__ import annotations

from eval.layer3.run_layer3_eval import score_case


def _base_pred(**overrides):
    pred = {
        "case_id": "case",
        "dataset": "unit",
        "coverage_tags": [],
        "query_time": "2026-08-03T00:00:00Z",
        "retrieved_refs": [],
        "answer": "",
        "answer_result": {"answer_type": "no_answer"},
        "answer_source_citations": [],
        "access_context": {"requester": "owner", "allow_sensitive": True},
        "memory_snapshot_input": {"memories": [], "versions": [], "sources": []},
        "expected": {
            "relevant_current_refs": [],
            "relevant_history_refs": [],
            "must_not_return_refs": [],
            "graded_relevance": {},
            "expected_claims": [],
            "required_citation_refs": [],
            "no_answer": True,
        },
        "pre_snapshot": {},
        "post_snapshot": {},
        "errors": [],
    }
    pred.update(overrides)
    return pred


def test_stage0_scores_irrelevant_must_not_separately_from_stale():
    pred = _base_pred(
        retrieved_refs=["m1"],
        memory_snapshot_input={
            "memories": [
                {"memory_ref": "m1", "status": "active", "sensitivity": "normal", "content": "用户喜欢咖啡"}
            ],
            "versions": [],
            "sources": [],
        },
        expected={
            "relevant_current_refs": [],
            "relevant_history_refs": [],
            "must_not_return_refs": ["m1"],
            "graded_relevance": {},
            "expected_claims": [],
            "required_citation_refs": [],
            "no_answer": True,
        },
    )

    scored = score_case(pred)

    assert scored["retrieval"]["must_not_return_violation"] is True
    assert scored["retrieval"]["irrelevant_retrieval"] is True
    assert scored["retrieval"]["irrelevant_refs"] == ["m1"]
    assert scored["retrieval"]["stale_retrieval_violation"] is False


def test_stage0_scores_superseded_as_stale_not_irrelevant():
    pred = _base_pred(
        retrieved_refs=["m1"],
        memory_snapshot_input={
            "memories": [
                {"memory_ref": "m1", "status": "superseded", "sensitivity": "normal", "content": "用户曾居住在新加坡"}
            ],
            "versions": [],
            "sources": [],
        },
        expected={
            "relevant_current_refs": [],
            "relevant_history_refs": [],
            "must_not_return_refs": ["m1"],
            "graded_relevance": {},
            "expected_claims": [],
            "required_citation_refs": [],
            "no_answer": False,
        },
    )

    scored = score_case(pred)

    assert scored["retrieval"]["must_not_return_violation"] is True
    assert scored["retrieval"]["stale_retrieval_violation"] is True
    assert scored["retrieval"]["stale_refs"] == ["m1"]
    assert scored["retrieval"]["irrelevant_retrieval"] is False


def test_stage0_scores_ambiguous_candidate_usage_separately():
    pred = _base_pred(
        coverage_tags=["ambiguous_reference"],
        retrieved_refs=["m1", "m2"],
        memory_snapshot_input={
            "memories": [
                {"memory_ref": "m1", "status": "active", "sensitivity": "normal", "content": "完善第一阶段评测"},
                {"memory_ref": "m2", "status": "active", "sensitivity": "normal", "content": "完善第二阶段评测"},
            ],
            "versions": [],
            "sources": [],
        },
        expected={
            "answer_type": "clarification",
            "relevant_current_refs": [],
            "relevant_history_refs": [],
            "must_not_return_refs": ["m1", "m2"],
            "graded_relevance": {},
            "expected_claims": [],
            "required_citation_refs": [],
            "no_answer": True,
        },
    )

    scored = score_case(pred)

    assert scored["retrieval"]["must_not_return_violation"] is False
    assert scored["retrieval"]["ambiguous_candidate_usage"] is True
    assert scored["retrieval"]["ambiguous_candidate_refs"] == ["m1", "m2"]
    assert scored["retrieval"]["irrelevant_retrieval"] is False


def test_stage0_non_fact_answer_evidence_sources_are_not_factual_citation_fp():
    pred = _base_pred(
        answer="我找到多个可能对象，请补充具体主题。",
        answer_result={"answer_type": "clarification", "reason_code": "ambiguous_candidates"},
        answer_source_citations=["s1", "s2"],
        expected={
            "answer_type": "clarification",
            "relevant_current_refs": [],
            "relevant_history_refs": [],
            "must_not_return_refs": [],
            "graded_relevance": {},
            "expected_claims": [],
            "required_citation_refs": [],
            "no_answer": False,
        },
    )

    scored = score_case(pred)

    assert scored["citation"]["actual"] == []
    assert scored["citation"]["fp"] == 0


def test_stage0_restricted_prediction_does_not_require_sensitive_leak():
    pred = _base_pred(
        answer="这条记录受访问权限保护，当前请求无权查看具体内容。",
        answer_result={"answer_type": "restricted", "reason_code": "acl_filtered_all_evidence"},
        access_context={"requester": "guest", "allow_sensitive": False},
        memory_snapshot_input={
            "memories": [
                {"memory_ref": "m1", "status": "active", "sensitivity": "sensitive", "content": "身份证号 123456"}
            ],
            "versions": [],
            "sources": [],
        },
        expected={
            "relevant_current_refs": [],
            "relevant_history_refs": [],
            "must_not_return_refs": ["m1"],
            "graded_relevance": {},
            "expected_claims": [],
            "required_citation_refs": [],
            "no_answer": True,
        },
    )

    scored = score_case(pred)

    assert scored["access"]["restricted_expected"] is True
    assert scored["access"]["restricted_predicted"] is True
    assert scored["access"]["violation"] is False
    assert scored["access"]["sensitive_answer_leak"] is False


def test_stage0_marks_unexposed_selected_context_contract_unavailable():
    pred = _base_pred()

    scored = score_case(pred)

    assert scored["stage0_contract"]["selected_context_refs_status"] == "unavailable"
    assert scored["stage0_contract"]["selected_tool_refs_status"] == "unavailable"
    assert scored["stage0_contract"]["executed_tools_status"] == "unavailable"
