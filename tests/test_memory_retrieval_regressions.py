"""Regression tests for Memory channel fusion and Ask routing."""

from __future__ import annotations

from math import isclose
from types import SimpleNamespace

import pytest

from memory.models import MemoryRecord, MemorySource, normalize_content


def _record(
    identifier: str,
    *,
    content: str | None = None,
    memory_type: str = "semantic",
    status: str = "active",
    access_count: int = 0,
    valid_from: str | None = None,
    canonical_topic: str | None = None,
    task_status: str | None = None,
    updated_at: str = "2026-08-01T00:00:00+00:00",
    current_version: int = 1,
    memory_key: str | None = None,
    source_count: int = 0,
) -> MemoryRecord:
    text = content or identifier
    return MemoryRecord(
        id=identifier,
        space_id="test",
        memory_type=memory_type,
        content=text,
        normalized_content=normalize_content(text),
        importance=0.8,
        confidence=0.9,
        status=status,
        valid_from=valid_from,
        valid_until=None,
        created_at="2026-08-01T00:00:00+00:00",
        updated_at=updated_at,
        last_accessed_at=None,
        access_count=access_count,
        current_version=current_version,
        task_status=task_status,
        sources=[
            MemorySource(
                memory_id=identifier,
                note_id=f"note-{index}",
                relation="created_from",
                created_at="2026-08-01T00:00:00+00:00",
            )
            for index in range(source_count)
        ],
        subject="用户",
        predicate=canonical_topic or "topic",
        object_value=text,
        memory_key=memory_key,
        scope={"canonical_topic": canonical_topic} if canonical_topic else {},
    )


def _row(identifier: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier,
        memory_key=None,
        subject="用户",
        predicate="topic",
        updated_at="2026-08-01T00:00:00+00:00",
        record=_record(identifier),
    )


def _patch_lightweight_record(monkeypatch):
    from repositories.postgres import memory as pg_memory

    monkeypatch.setattr(
        pg_memory,
        "_record",
        lambda _session, row, **_kwargs: row.record,
    )
    monkeypatch.setattr(pg_memory, "RETRIEVAL_WEIGHTED_RRF_ENABLED", True)
    monkeypatch.setattr(pg_memory, "MEMORY_HYBRID_RRF_K", 60)
    return pg_memory


def test_rrf_duplicate_channel_contributes_only_once(monkeypatch):
    pg_memory = _patch_lightweight_record(monkeypatch)
    first, second = _row("first"), _row("second")

    hits = pg_memory._rrf_hits([
        ("structured_slot", [first, second]),
        ("structured_slot", [first]),
    ])
    hit = next(item for item in hits if item.memory.id == "first")

    assert hit.channel_ranks == {"structured_slot": 1}
    assert isclose(hit.rrf_score, 1.35 / 61, rel_tol=1e-9)


def test_family_is_an_independent_corroborating_channel(monkeypatch):
    pg_memory = _patch_lightweight_record(monkeypatch)
    row = _row("task")

    hit = pg_memory._rrf_hits([
        ("structured_slot", [row]),
        ("family", [row]),
    ])[0]

    assert hit.family_rank == 1
    assert pg_memory._hit_channel_count(hit) == 2


def test_vector_weight_scales_down_when_coverage_is_partial(monkeypatch):
    pg_memory = _patch_lightweight_record(monkeypatch)
    rows = [_row(f"row-{index}") for index in range(20)]

    hit = next(
        item
        for item in pg_memory._rrf_hits([
            ("lexical", rows),
            ("vector", [rows[-1]]),
        ], limit=20)
        if item.memory.id == rows[-1].id
    )

    assert isclose(hit.vector_score, 0.95 * 0.25 / 61, rel_tol=1e-9)
    assert "vector_coverage_scaled" in hit.reasons


def test_strong_lexical_terms_keep_identity_and_drop_generic_event_words():
    from repositories.postgres.memory import _strong_lexical_terms

    terms = _strong_lexical_terms("我参加过的健身房体验课发生了什么？")

    assert "体验课" in terms
    assert "参加过" not in terms
    assert "发生" not in terms
    assert all(len(normalize_content(term)) >= 3 for term in terms)


def test_type_hint_is_not_an_independent_rrf_channel():
    """A guessed type must not add relevance votes to unrelated memories."""
    from repositories.postgres.memory import _RRF_CHANNEL_WEIGHTS

    assert "type_hint" not in _RRF_CHANNEL_WEIGHTS


def test_task_current_precedence_prefers_authoritative_version_not_done_value():
    from memory.retrieval_models import MemoryRetrievalHit
    from repositories.postgres.memory import _apply_task_current_precedence

    stale_todo = _record(
        "stale-todo", content="上下文工程实验待处理", memory_type="task",
        canonical_topic="上下文工程实验", task_status="todo", current_version=1,
        memory_key="task:用户:上下文工程实验:todo",
    )
    current_done = _record(
        "current-done", content="上下文工程实验已完成", memory_type="task",
        canonical_topic="上下文工程实验", task_status="done", current_version=3,
        memory_key="task:用户:上下文工程实验:done",
    )
    stale_hit = MemoryRetrievalHit(memory=stale_todo, final_score=0.958)
    current_hit = MemoryRetrievalHit(memory=current_done, final_score=0.958)

    adjusted = _apply_task_current_precedence([
        (stale_todo, 0.958, stale_hit),
        (current_done, 0.958, current_hit),
    ])
    scores = {memory.id: score for memory, score, _hit in adjusted}

    assert scores["current-done"] > scores["stale-todo"]
    assert "task_current_authoritative" in current_hit.reasons
    assert "task_current_superseded" in stale_hit.reasons


def test_task_current_precedence_allows_newer_todo_to_reopen_done_task():
    from memory.retrieval_models import MemoryRetrievalHit
    from repositories.postgres.memory import _apply_task_current_precedence

    old_done = _record(
        "old-done", content="论文答辩已完成", memory_type="task",
        canonical_topic="论文答辩", task_status="done", current_version=3,
        updated_at="2026-08-01T00:00:00+00:00",
    )
    reopened_todo = _record(
        "reopened-todo", content="论文答辩需要重新修改", memory_type="task",
        canonical_topic="论文答辩", task_status="todo", current_version=4,
        updated_at="2026-08-10T00:00:00+00:00",
    )
    done_hit = MemoryRetrievalHit(memory=old_done, final_score=0.99)
    reopened_hit = MemoryRetrievalHit(memory=reopened_todo, final_score=0.90)

    adjusted = _apply_task_current_precedence([
        (old_done, 0.99, done_hit),
        (reopened_todo, 0.90, reopened_hit),
    ])
    scores = {memory.id: score for memory, score, _hit in adjusted}

    assert scores["reopened-todo"] > scores["old-done"]
    assert "task_current_authoritative" in reopened_hit.reasons


def test_task_current_precedence_does_not_guess_when_authority_ties():
    from memory.retrieval_models import MemoryRetrievalHit
    from repositories.postgres.memory import _apply_task_current_precedence

    todo = _record(
        "todo", content="发布报告待处理", memory_type="task",
        canonical_topic="发布报告", task_status="todo", current_version=2,
    )
    done = _record(
        "done", content="发布报告已完成", memory_type="task",
        canonical_topic="发布报告", task_status="done", current_version=2,
    )
    todo_hit = MemoryRetrievalHit(memory=todo, final_score=0.96)
    done_hit = MemoryRetrievalHit(memory=done, final_score=0.95)

    adjusted = _apply_task_current_precedence([
        (todo, 0.96, todo_hit),
        (done, 0.95, done_hit),
    ])
    scores = {memory.id: score for memory, score, _hit in adjusted}

    assert scores == {"todo": 0.96, "done": 0.95}
    assert "task_current_state_ambiguous" in todo_hit.reasons
    assert "task_current_state_ambiguous" in done_hit.reasons


def test_task_read_precedence_gate_allows_task_intent_without_type_filter():
    from repositories.postgres.memory import _task_read_precedence_authorized

    assert _task_read_precedence_authorized(
        "总结这个项目从开始到完成的过程",
        memory_type=None,
        time_mode="history",
    )
    assert _task_read_precedence_authorized(
        "这个任务现在是什么状态",
        memory_type=None,
        time_mode="current",
    )


def test_task_read_precedence_gate_rejects_cross_type_current_fact():
    from repositories.postgres.memory import _task_read_precedence_authorized

    assert not _task_read_precedence_authorized(
        "算法训练计划现在还算重点吗",
        memory_type=None,
        time_mode="current",
    )


def test_task_read_precedence_applies_to_mixed_search_without_type_filter():
    from memory.retrieval_models import MemoryRetrievalHit
    from repositories.postgres.memory import _apply_task_read_precedence

    stale_done = _record(
        "stale-done", content="发布报告已完成", memory_type="task",
        canonical_topic="发布报告", task_status="done", current_version=2,
        updated_at="2026-08-01T00:00:00+00:00",
    )
    reopened_todo = _record(
        "reopened-todo", content="发布报告需要修改", memory_type="task",
        canonical_topic="发布报告", task_status="todo", current_version=3,
        updated_at="2026-08-10T00:00:00+00:00",
    )
    unrelated = _record("semantic-row", content="用户住在北京")
    stale_hit = MemoryRetrievalHit(memory=stale_done, final_score=0.99)
    reopened_hit = MemoryRetrievalHit(memory=reopened_todo, final_score=0.90)
    semantic_hit = MemoryRetrievalHit(memory=unrelated, final_score=0.80)

    adjusted = _apply_task_read_precedence([
        (stale_done, 0.99, stale_hit),
        (reopened_todo, 0.90, reopened_hit),
        (unrelated, 0.80, semantic_hit),
    ], time_mode="current")
    scores = {memory.id: score for memory, score, _hit in adjusted}

    assert scores["reopened-todo"] > scores["stale-done"]
    assert scores["semantic-row"] == 0.80
    assert "task_current_authoritative" in reopened_hit.reasons


def test_task_history_precedence_prefers_rich_timeline_not_done_value():
    from memory.retrieval_models import MemoryRetrievalHit
    from repositories.postgres.memory import _apply_task_history_precedence

    shallow_done = _record(
        "shallow-done", content="上下文实验已完成", memory_type="task",
        canonical_topic="上下文实验", task_status="done", current_version=1,
        source_count=1,
    )
    rich_todo = _record(
        "rich-todo", content="上下文实验需要继续修改", memory_type="task",
        canonical_topic="上下文实验", task_status="todo", current_version=3,
        source_count=3,
    )
    done_hit = MemoryRetrievalHit(memory=shallow_done, final_score=0.96)
    todo_hit = MemoryRetrievalHit(memory=rich_todo, final_score=0.95)

    adjusted = _apply_task_history_precedence([
        (shallow_done, 0.96, done_hit),
        (rich_todo, 0.95, todo_hit),
    ])
    scores = {memory.id: score for memory, score, _hit in adjusted}

    assert scores["rich-todo"] > scores["shallow-done"]
    assert "task_history_representative" in todo_hit.reasons
    assert "task_history_nonrepresentative" in done_hit.reasons


@pytest.mark.parametrize(
    "query",
    ["这个任务经历了哪些变化？", "请总结该项目的版本演进", "show the project timeline"],
)
def test_build_memory_query_spec_detects_history_intent(query: str):
    from memory.service import build_memory_query_spec

    assert build_memory_query_spec(query).time_mode == "history"


def test_build_memory_query_spec_keeps_current_fact_intent():
    from memory.service import build_memory_query_spec

    assert build_memory_query_spec("我现在住在哪里？").time_mode == "current"


def test_semantic_current_precedence_uses_business_time_within_identity():
    from memory.retrieval_models import MemoryRetrievalHit
    from repositories.postgres.memory import _apply_semantic_current_precedence

    history = _record(
        "history", content="用户过去的常住地是上海", memory_type="semantic",
        valid_from="2026-06-01T00:00:00+00:00", canonical_topic="常住地",
    )
    current = _record(
        "current", content="用户现在的常住地是北京", memory_type="semantic",
        valid_from="2026-08-01T00:00:00+00:00", canonical_topic="常住地",
    )
    history_hit = MemoryRetrievalHit(memory=history, final_score=0.98)
    current_hit = MemoryRetrievalHit(memory=current, final_score=0.975)

    adjusted = _apply_semantic_current_precedence(
        [
            (history, 0.98, history_hit),
            (current, 0.975, current_hit),
        ],
        query="我目前的常住地是哪里？",
    )
    scores = {memory.id: score for memory, score, _hit in adjusted}

    assert scores["current"] > scores["history"]
    assert "semantic_current_latest_boost" in current_hit.reasons
    assert "semantic_current_history_penalty" in history_hit.reasons


def test_status_penalties_multiply_and_access_count_does_not_rank():
    from memory.retriever import score_memory

    query = "毕业论文任务状态"
    active = _record("active", content="毕业论文任务", status="active")
    conflicted = _record("conflicted", content="毕业论文任务", status="conflicted")
    popular = _record("popular", content="毕业论文任务", access_count=100)

    assert score_memory(query, conflicted) == pytest.approx(score_memory(query, active) * 0.5, abs=0.0002)
    assert score_memory(query, popular) == score_memory(query, active)


def test_current_task_fast_route_uses_task_status_search():
    from agent.query_agent import _deterministic_route

    route = _deterministic_route("我的毕业论文任务进展如何")
    assert route is not None
    assert route["action"] == "task_status_search"


def test_semantic_search_fallback_remains_note_lookup(monkeypatch):
    from agent import ask_planner, query_agent

    monkeypatch.setattr(
        query_agent,
        "_deterministic_route",
        lambda _question: {"action": "semantic_search", "args": {"query": "自然语言"}},
    )
    plan = ask_planner.deterministic_fallback_plan("自然语言问题")

    assert plan.units[0].intent == "note_lookup"
    assert plan.units[0].memory_type is None


def test_variant_fusion_keeps_original_and_accepts_secondary_lane():
    from agent.ask_executor import _merge_records

    primary = [{"id": f"primary-{index}"} for index in range(8)]
    secondary = [{"id": f"secondary-{index}"} for index in range(3)]
    merged = _merge_records([primary, secondary], limit=8)
    ids = [item["id"] for item in merged]

    assert ids[:4] == [f"primary-{index}" for index in range(4)]
    assert "secondary-0" in ids


def test_query_spec_projects_task_family_without_authorizing_updates():
    from memory.service import build_memory_query_spec

    spec = build_memory_query_spec(
        "我的毕业论文任务进展如何",
        memory_type="task",
        canonical_topic="毕业论文答辩",
        time_mode="current",
    )

    assert spec.canonical_topic == "毕业论文答辩"
    assert spec.family_key == "task-family:毕业论文答辩"
    assert spec.time_mode == "current"


def test_query_spec_infers_current_time_mode_from_user_wording():
    from memory.service import build_memory_query_spec

    spec = build_memory_query_spec("关于常住地，目前记录的最新事实是什么？", memory_type="semantic")

    assert spec.time_mode == "current"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("截至现在，常住地应以哪条记录为准？", "常住地"),
        ("到目前为止，工作地点哪个值才是当前事实？", "工作地点"),
        ("至今，常用语言应当以哪项信息作为最新记录为准？", "常用语言"),
    ],
)
def test_retrieval_topic_text_removes_current_selection_scaffolding(query, expected):
    from memory.retriever import retrieval_topic_text

    assert retrieval_topic_text(query) == expected
