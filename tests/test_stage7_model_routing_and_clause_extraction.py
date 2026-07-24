from __future__ import annotations

from core.model_router import route_model
from apps.handlers import HANDLERS
from memory.clause_splitter import split_clauses
from memory.extractor import extract_rule_candidates, may_contain_memory
from runtime.streams.client import GROUPS


def test_model_router_maps_tasks_to_expected_roles(monkeypatch):
    monkeypatch.setattr("core.settings.MODEL_ROUTING_ENABLED", True)
    monkeypatch.setattr("core.settings.STRONG_ESCALATION_ENABLED", False)

    assert route_model(task="note_classification").role.value == "fast"
    assert route_model(task="memory_extraction").role.value == "fast"
    assert route_model(task="query_synthesis").role.value == "balanced"
    assert route_model(task="query_complex_reasoning").role.value == "balanced"
    assert route_model(task="summary_review", range_key="day").role.value == "balanced"
    assert route_model(task="summary_review", range_key="month").role.value == "balanced"

    monkeypatch.setattr("core.settings.STRONG_ESCALATION_ENABLED", True)
    assert route_model(task="query_complex_reasoning").role.value == "strong"
    assert route_model(task="summary_review", range_key="month").role.value == "strong"


def test_short_fact_admission_gate_covers_compact_user_facts():
    examples = [
        "我是杭州人",
        "我姓张",
        "我有两个姐姐",
        "我养了一只猫",
        "我会弹吉他",
        "我的生日是七月二十日",
    ]

    for text in examples:
        assert may_contain_memory(text), text


def test_clause_splitter_keeps_evidence_spans():
    clauses = split_clauses("今天参加了交流会，我更喜欢小班练习，下周还要报名。")

    assert [item.text for item in clauses] == ["今天参加了交流会", "我更喜欢小班练习", "下周还要报名"]
    assert [item.index for item in clauses] == [0, 1, 2]


def test_clause_level_rule_extraction_emits_multiple_candidate_types(monkeypatch):
    monkeypatch.setattr("core.settings.MEMORY_CLAUSE_EXTRACTION_ENABLED", True)

    candidates = extract_rule_candidates("note-stage7", "今天参加了交流会，我更喜欢小班练习，下周还要报名。")

    assert {candidate.memory_type for candidate in candidates} >= {"episodic", "preference", "task"}
    assert all(candidate.evidence_span for candidate in candidates)
    assert all(candidate.clause_index is not None for candidate in candidates)
    assert len({candidate.candidate_id for candidate in candidates}) == len(candidates)


def test_memory_embedding_worker_route_is_registered():
    assert "memory_embedding" in HANDLERS
    assert GROUPS["memory_embedding"] == "memory-embedding-workers"
