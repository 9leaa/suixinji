from memory.candidate_validator import contains_sensitive_data, validate_candidate
from memory.models import MemoryCandidate


def test_candidate_validator_rejects_sensitive_note_even_when_candidate_looks_safe():
    candidate = MemoryCandidate("semantic", "用户正在开发项目", 0.8, 0.9)

    checked, rejection = validate_candidate(candidate, note_text="项目密码: abc123456")

    assert checked is None
    assert rejection.reason == "sensitive_data"


def test_candidate_validator_clamps_scores_and_deduplicates_entities():
    candidate = MemoryCandidate(
        "semantic",
        "用户正在开发随心记项目",
        3.0,
        2.0,
        entities=["Agent", "Agent", "RAG"],
        evidence_span="开发随心记项目",
    )

    checked, rejection = validate_candidate(candidate, note_text="我正在开发随心记项目")

    assert rejection is None
    assert checked.confidence == 1.0
    assert checked.importance == 1.0
    assert checked.entities == ["Agent", "RAG"]


def test_sensitive_pattern_detection_handles_long_financial_numbers():
    assert contains_sensitive_data("账号 6222021234567890123") is True
