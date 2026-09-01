from agent.note_reranker import (
    fuse_note_variants,
    parse_query_constraints,
    rerank_note_records,
    select_answer_evidence,
)


def test_assistant_cue_overrides_first_person_wording() -> None:
    constraints = parse_query_constraints("I remember you told me about the refinery.")
    assert constraints.role_mode == "assistant"



def test_comparison_query_requires_multiple_evidence() -> None:
    constraints = parse_query_constraints("Do I go to the gym more frequently than I did previously?")
    assert constraints.needs_multi_evidence
def test_query_constraint_rerank_can_correct_a_nearby_rrf_rank() -> None:
    records = [
        {"id": "generic", "score": 0.90, "text": "A generic travel recommendation."},
        {"id": "target", "score": 0.84, "text": "You recommended an Amsterdam hostel near the Red Light District."},
    ]
    ranked = rerank_note_records(
        "What hostel did you recommend near the Red Light District?",
        records,
        roles_by_id={"generic": ["user"], "target": ["assistant"]},
    )
    assert ranked[0]["id"] == "target"
    assert ranked[0]["v2_constraint_coverage"] > ranked[1]["v2_constraint_coverage"]


def test_rewrite_lane_cannot_outvote_original_wording_by_default() -> None:
    records = fuse_note_variants(
        [
            [{"id": "original", "ts": "2026-01-01"}],
            [{"id": "rewrite", "ts": "2026-01-02"}],
        ],
        limit=2,
    )
    assert records[0]["id"] == "original"


def test_multi_evidence_keeps_best_result_first() -> None:
    records = [
        {"id": "best", "score": 0.90, "text": "Marvel movies took six weeks."},
        {"id": "duplicate", "score": 0.88, "text": "Marvel movies took six weeks with a complete list."},
        {"id": "complement", "score": 0.82, "text": "Star Wars films took four weeks."},
    ]
    ranked = rerank_note_records(
        "How many weeks did both Marvel movies and Star Wars films take in total?",
        records,
    )
    assert ranked[0]["id"] == "best"
    assert {item["id"] for item in ranked[:2]} != {"best", "duplicate"}


def test_answer_evidence_selection_requires_identity_coverage() -> None:
    selected = select_answer_evidence(
        "What did I do with the signed football collection?",
        [
            {"id": "baseball", "text": "I reorganized my signed baseball collection.", "ts": "2026-01-03"},
            {"id": "football", "text": "I catalogued my signed football collection by team.", "ts": "2026-01-02"},
            {"id": "generic", "text": "I bought storage boxes for my collection.", "ts": "2026-01-04"},
        ],
        limit=5,
    )
    assert selected[0]["id"] == "football"
    assert len(selected) == 3


def test_answer_evidence_selection_prefers_newer_matching_current_fact() -> None:
    selected = select_answer_evidence(
        "How many Instagram followers do I have now?",
        [
            {"id": "old", "text": "I have 1250 Instagram followers.", "ts": "2026-01-01"},
            {"id": "new", "text": "I now have 1300 Instagram followers.", "ts": "2026-02-01"},
        ],
        limit=3,
    )
    assert selected[0]["id"] == "new"
