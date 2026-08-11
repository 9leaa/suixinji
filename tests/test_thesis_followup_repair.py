import json

from memory import extractor, service
from memory.canonicalizer import canonicalize_candidate
from memory.consolidator import convert_orphan_done_task_to_episodic
from memory.models import MemoryCandidate
from memory.repository import get_extraction_state, list_memories
from memory.service import process_note_memory, task_status_search


def test_tentative_language_is_not_a_message_level_skip(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "rules")
    text = "数据同步中断。后续可能需要人工处理"

    candidates = extractor.extract_candidates("thesis-problem", text)

    # Rule fallback does not manufacture a task from a modal-only clause; the
    # complete message remains eligible for Hybrid extraction.
    assert candidates == []
    assert extractor.may_contain_memory(text)


def test_standalone_modal_clause_is_rejected_without_domain_wordlists():
    assert extractor._is_tentative_only_clause("现在可能需要人工处理")
    assert extractor._is_tentative_only_clause("我也许会调整计划")
    assert not extractor._is_tentative_only_clause("数据同步中断")


def test_recurrence_signal_includes_recent_messages_for_llm(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    captured: dict[str, object] = {}

    def fake_complete_json(**kwargs):
        captured.update(json.loads(kwargs["user_prompt"]))
        return {"candidates": []}

    monkeypatch.setattr(extractor, "complete_json", fake_complete_json)
    extractor.extract_candidates(
        "thesis-followup",
        "我毕业论文答辩又出现了问题",
        previous_messages=[{"note_id": "thesis-done", "text": "我完成了本科毕业论文的答辩"}],
    )

    assert captured["previous_messages"] == [{"note_id": "thesis-done", "text": "我完成了本科毕业论文的答辩", "offset": -1}]


def test_orphan_completion_keeps_read_only_family_anchor():
    candidate = canonicalize_candidate(
        MemoryCandidate(
            memory_type="task",
            content="我完成了本科毕业论文的答辩",
            importance=0.9,
            confidence=0.9,
            task_status="done",
            note_id="thesis-done",
            subject="用户",
            predicate="本科毕业论文答辩",
            object_value="本科毕业论文答辩",
            evidence_span="我完成了本科毕业论文的答辩",
            scope={"operation": "执行", "canonical_topic": "执行本科毕业论文答辩", "scope": "global"},
        )
    )

    event = convert_orphan_done_task_to_episodic(candidate)

    assert event.memory_type == "episodic"
    assert "task_family_key" not in event.scope
    assert event.scope["related_task_family_key"] == candidate.scope["task_family_key"]


def test_explicit_current_assertion_gets_its_own_identity_but_keeps_related_family():
    candidate = MemoryCandidate(
        memory_type="task",
        content="数据同步中断，后续可能需要人工处理",
        importance=0.9,
        confidence=0.9,
        task_status="todo",
        note_id="sync-followup",
        subject="用户",
        predicate="完成数据平台迁移",
        object_value="完成数据平台迁移",
        evidence_span="数据同步中断",
        scope={
            "operation": "维护",
            "canonical_topic": "执行数据平台迁移",
            "scope": "global",
            "reference_status": "resolved",
            "task_family_key": "task-family:数据平台迁移",
        },
    )

    normalized = canonicalize_candidate(candidate)

    assert normalized.predicate == "数据同步中断"
    assert normalized.scope["identity_source"] == "current_evidence"
    assert normalized.scope["related_task_family_key"] == "task-family:数据平台迁移"
    assert normalized.scope["task_family_key"] != "task-family:数据平台迁移"


def test_task_status_search_filters_low_score_distractors_and_keeps_history(monkeypatch):
    def fake_memory_search(_space_id, _query, *, memory_type=None, **_kwargs):
        if memory_type == "task":
            return [
                {
                    "id": "unrelated-task",
                    "memory_type": "task",
                    "content": "Agent 简历已经完成",
                    "score": 0.48,
                    "scope": {"task_family_key": "task-family:agent简历"},
                }
            ]
        return [
            {
                "id": "thesis-event",
                "memory_type": "episodic",
                "content": "我完成了本科毕业论文的答辩",
                "score": 0.74,
                "scope": {"related_task_family_key": "task-family:本科毕业论文答辩"},
            }
        ]

    monkeypatch.setattr(service, "memory_search", fake_memory_search)
    results = task_status_search("space", "我毕业论文事宜怎么样了")

    assert [item["id"] for item in results] == ["thesis-event"]
    assert results[0]["task_evidence_role"] == "historical_event"


def test_task_status_query_anchor_deduplicates_route_appended_topic():
    assert service._task_status_query_anchor("数据迁移的进展怎么样 数据迁移") == "数据迁移"


def test_force_replay_can_replace_a_terminal_empty_state(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "rules")
    note_id = "force-replay-empty"
    space_id = "force-replay-space"
    process_note_memory({"id": note_id, "space_id": space_id, "text": "可能以后会学 Java"})
    assert get_extraction_state(note_id).status == "empty"

    report = process_note_memory(
        {"id": note_id, "space_id": space_id, "text": "记得完成毕业论文修改"},
        force=True,
    )

    assert report["extraction_status"] == "completed"
    assert list_memories(space_id, status="active", memory_type="task")
