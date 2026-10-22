"""Source edits invalidate derived evidence even when the replacement extracts nothing."""
from core import settings
from memory import fact_memory as fm, service
import pytest


def test_empty_replacement_invalidates_old_fact_and_card(monkeypatch):
    monkeypatch.setattr(settings, "FACT_MEMORY_ENABLED", True)
    old = fm.ingest_fact("s", note_id="edited-note", original_text="样品保存在冷藏柜。",
                         content="样品保存在冷藏柜。", memory_type="semantic", enqueue=False)
    fm.consolidate("s", "semantic", planner=lambda *args: {"actions": [{
        "action": "create", "content": old["content"], "memory_type": "semantic",
        "assertion_kind": "stated", "source_fact_ids": [old["id"]], "reason": "原文支持",
    }]})
    monkeypatch.setattr(service, "may_contain_memory", lambda *args, **kwargs: False)
    service.process_note_memory({"id": "edited-note", "space_id": "s", "text": "谢谢。"})
    assert fm.get("s", "fact", old["id"])["status"] == "source_changed"
    assert not fm.cards("s")
    # Returning to an earlier source revision is a new extraction opportunity.
    restored = fm.ingest_fact("s", note_id="edited-note", original_text=old["original_text"],
                             content=old["content"], memory_type="semantic", enqueue=False)
    assert restored["status"] == "active"
    assert restored["processed_at"] is None


def test_model_failure_cannot_be_marked_empty(monkeypatch):
    monkeypatch.setattr(settings, "FACT_MEMORY_ENABLED", True)
    monkeypatch.setattr(service, "MEMORY_EXTRACTOR_MODE", "llm")
    def fail(*args, **kwargs):
        assert kwargs["allow_llm_failure_fallback"] is False
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(service, "extract_candidates", fail)
    with pytest.raises(RuntimeError):
        service.process_note_memory({"id": "failed-note", "space_id": "s", "text": "这条值得保存。"})
    assert service.get_extraction_state("failed-note").status == "failed"


def test_task_candidate_does_not_skip_independent_current_state_extraction(monkeypatch):
    from memory import extractor
    monkeypatch.setattr(settings, "FACT_MEMORY_ENABLED", True)
    calls = []
    def model(**kw):
        calls.append(kw)
        if kw.get('llm_task'):
            return {"candidates": [{"memory_type": "task", "task": "联系供应商",
                    "description": "明天联系供应商", "evidence_span": "明天联系供应商",
                    "task_status": "todo", "importance": .8, "confidence": .9}]}
        return {"candidates": [{"memory_type": "semantic", "content": "当前还没有收到样品",
                "evidence_span": "当前还没有收到样品", "importance": .8, "confidence": .9}]}
    monkeypatch.setattr(extractor, "complete_json", model)
    rows = extractor.extract_llm_candidates("n", "明天联系供应商，当前还没有收到样品")
    assert len(calls) == 2
    assert {r.memory_type for r in rows} == {"task", "semantic"}
    assert any(r.content == "当前还没有收到样品" for r in rows)
