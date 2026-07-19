from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from core.config import get_chat_config
from memory.consolidator import generate_stable_semantic
from memory.expiry import run_expiry_once
from memory.models import MemoryCandidate, MemoryDecision, candidate_id_for
from memory.repository import (
    apply_memory_decision,
    edit_pending_memory,
    get_memory_candidate_status,
    insert_memory,
    list_memories,
    list_retryable_memory_candidates,
    reject_pending_memory,
    resolve_memory_conflict,
    search_memories,
)
from memory.service import process_note_memory


def test_candidate_is_persisted_and_terminal_retry_is_idempotent() -> None:
    note = {"id": "note-candidate", "space_id": "space-1", "text": "我喜欢喝牛奶"}
    first = process_note_memory(note)
    second = process_note_memory(note)
    candidate_id = candidate_id_for(note["id"], "preference", "用户喜欢喝牛奶")

    assert first["extraction_status"] == "completed"
    assert second["idempotent"] is True
    assert get_memory_candidate_status(candidate_id) == "applied"
    assert list_retryable_memory_candidates("space-1") == []
    assert len(list_memories("space-1", memory_type="preference")) == 1


def test_model_roles_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("SUIXINJI_FAST_MODEL", "fast-test")
    monkeypatch.setenv("SUIXINJI_BALANCED_MODEL", "balanced-test")
    monkeypatch.setenv("SUIXINJI_STRONG_MODEL", "strong-test")

    assert get_chat_config("fast").model == "fast-test"
    assert get_chat_config("balanced").model == "balanced-test"
    assert get_chat_config("strong").model == "strong-test"


def test_partial_retry_skips_applied_candidate(monkeypatch) -> None:
    import memory.service as service

    first = MemoryCandidate("semantic", "用户正在学习数据库", 0.8, 0.9)
    second = MemoryCandidate("preference", "用户喜欢绿茶", 0.8, 0.9)
    calls: list[str] = []
    failed_once = {"value": False}

    monkeypatch.setattr(service, "may_contain_memory", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(service, "extract_candidates", lambda *args, **kwargs: [first, second])

    def consolidate(space_id, note_id, candidate, trace=None):
        calls.append(candidate.memory_type)
        if candidate.memory_type == "preference" and not failed_once["value"]:
            failed_once["value"] = True
            raise RuntimeError("temporary")
        return {"candidate_id": candidate.candidate_id, "decision_id": f"decision-{candidate.memory_type}", "action": "insert"}

    monkeypatch.setattr(service, "consolidate_candidate", consolidate)
    note = {"id": "note-partial", "space_id": "space-1", "text": "组合候选输入"}

    assert process_note_memory(note)["extraction_status"] == "partial"
    assert process_note_memory(note)["extraction_status"] == "completed"
    assert calls == ["semantic", "preference", "preference"]


def test_negative_preference_does_not_answer_positive_preference_query() -> None:
    process_note_memory({"id": "note-like", "space_id": "space-1", "text": "我喜欢喝牛奶"})
    process_note_memory({"id": "note-dislike", "space_id": "space-1", "text": "我讨厌喝牛奶"})

    assert search_memories("space-1", "我喜欢喝什么", mark_access=False) == []
    negative = search_memories("space-1", "我不喜欢喝什么", mark_access=False)
    assert negative and "讨厌喝牛奶" in negative[0][0].content


def test_valid_until_is_filtered_and_expiry_worker_versions_state() -> None:
    expired_at = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds")
    memory = insert_memory(
        "space-1",
        MemoryCandidate("semantic", "用户本周住在上海", 0.8, 0.9, valid_until=expired_at),
        source_note_id="note-expired",
    )

    assert list_memories("space-1", memory_type="semantic") == []
    report = run_expiry_once(space_id="space-1")
    expired = list_memories("space-1", status="expired", memory_type="semantic")

    assert report["expired_count"] == 1
    assert expired[0].id == memory.id
    assert expired[0].current_version == 2


def test_monthly_consolidation_is_domain_neutral() -> None:
    for index, text in enumerate(("周末练习烘焙面包", "记录一次跑步训练", "整理旅行照片")):
        insert_memory("space-1", MemoryCandidate("episodic", text, 0.7, 0.8), source_note_id=f"note-{index}")

    report = generate_stable_semantic("space-1", min_sources=3)
    semantic = list_memories("space-1", memory_type="semantic")

    assert report["created"] is True
    assert semantic
    assert "Agent/RAG" not in semantic[0].content
    assert all(text in semantic[0].content for text in ("烘焙", "跑步", "旅行"))


def _pending_memory(note_id: str, content: str) -> str:
    candidate = replace(
        MemoryCandidate("preference", content, 0.8, 0.9),
        note_id=note_id,
        candidate_id=candidate_id_for(note_id, "preference", content),
    )
    decision = MemoryDecision(
        candidate_id=candidate.candidate_id,
        relation="new",
        target_memory_ids=[],
        confidence=0.7,
        reason="manual_review_test",
        evidence=[f"note:{note_id}"],
        recommended_action="pending_review",
    )
    return str(apply_memory_decision("space-1", note_id, candidate, decision)["memory_id"])


def test_review_reject_edit_and_conflict_resolution() -> None:
    rejected_id = _pending_memory("note-reject", "用户喜欢咖啡")
    rejected = reject_pending_memory(rejected_id, reason="not_user_fact")
    assert rejected is not None and rejected.status == "archived"

    edited_id = _pending_memory("note-edit", "用户喜欢茶")
    edited = edit_pending_memory(edited_id, "用户喜欢绿茶")
    assert edited is not None and edited.status == "active" and "绿茶" in edited.content

    conflict = insert_memory("space-1", MemoryCandidate("semantic", "用户住在杭州", 0.8, 0.9), source_note_id="note-conflict", status="conflicted")
    resolved = resolve_memory_conflict(conflict.id, resolution="keep")
    assert resolved is not None and resolved.status == "active"
