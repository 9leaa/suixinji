from core import settings
from memory import fact_memory as fm
from agent import query_agent


def seed():
    facts = [fm.ingest_fact("s", note_id=str(i), original_text=t, content=t,
                           memory_type="episodic", enqueue=False)
             for i, t in enumerate(["取样瓶在上午封好。", "同次取样的取样瓶在下午送达。"])]
    fm.consolidate("s", "episodic", planner=lambda *args: {"actions": [{
        "action": "create", "memory_type": "episodic", "assertion_kind": "stated",
        "content": "同次取样的取样瓶上午封好，下午送达。",
        "source_fact_ids": [f["id"] for f in facts], "reason": "同次事件补充",
    }]})
    return facts


def test_history_keeps_new_store_evidence(monkeypatch):
    monkeypatch.setattr(settings, "FACT_MEMORY_ENABLED", True)
    seed()
    monkeypatch.setattr(query_agent, "_memory_search_compat",
                        lambda s, q, **kw: fm.query_hits(s, q, limit=10))
    rows = query_agent.memory_history("s", "取样瓶")
    assert rows and any("送达" in r["content"] for r in rows)
    assert all(r.get("sources") for r in rows)


def test_recent_events_do_not_count_card_and_sources_as_separate_events(monkeypatch):
    monkeypatch.setattr(settings, "FACT_MEMORY_ENABLED", True)
    seed()
    rows = query_agent.list_recent_episodes("s")
    assert len(rows) == 1
    assert "上午" in rows[0]["content"] and "下午" in rows[0]["content"]


def test_note_tool_fallback_masks_forgotten_spans_and_generated_metadata(monkeypatch):
    monkeypatch.setattr(settings, "FACT_MEMORY_ENABLED", True)
    original = "藏品编号是M-77。无关说明保留。"
    f = fm.ingest_fact("s", note_id="n", original_text=original, evidence_span="藏品编号是M-77。",
                       content="藏品编号是M-77。", memory_type="semantic", enqueue=False)
    preview = fm.request_forget("s", "M-77")
    fm.approve_forget("s", preview["id"], [f["id"]])
    raw = {"id": "n", "text": original, "title": "M-77", "summary": "编号M-77", "tags": ["M-77"]}
    monkeypatch.setattr(query_agent, "_execute_tool", lambda *args, **kwargs: [raw])
    rows = query_agent._run_tool("s", "memory_note_fallback", {"query": "编号"})
    assert "M-77" not in str(rows)
    assert "无关说明保留" in rows[0]["text"]
    assert raw["text"] == original


def test_semantic_channel_recalls_without_shared_words(monkeypatch):
    from memory import fact_retrieval
    monkeypatch.setattr(settings, "FACT_MEMORY_SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(fact_retrieval, "embed_text", lambda text: [1., 0.] if text in {"query", "meaning"} else [0., 1.])
    rows = [{"id": "match", "content": "meaning"}, {"id": "other", "content": "noise"}]
    result = fact_retrieval.rank("s", "query", rows, 1)
    assert result[0]["id"] == "match"
    assert result[0]["lexical_score"] == 0
    assert "semantic" in result[0]["retrieval_channels"]


def test_synthesis_failure_preserves_all_distinct_fact_evidence(monkeypatch):
    monkeypatch.setattr(settings, "FACT_MEMORY_ENABLED", True)
    def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(query_agent, "_complete_json_with_hooks", fail)
    result = query_agent._synthesize_answer("检查与处理有哪些？", [{
        "tool": "memory_history",
        "result": [{"id": f"fact_{i}", "content": f"已记录事件{i}"} for i in range(6)],
    }])
    assert "暂时未能生成综合回答" in result
    assert all(f"已记录事件{i}" in result for i in range(6))


def test_query_limit_counts_distinct_evidence_not_fact_card_duplicates(monkeypatch):
    monkeypatch.setattr(settings, "FACT_MEMORY_ENABLED", True)
    facts = [fm.ingest_fact("s", note_id=str(i), original_text=text, content=text,
                           memory_type="semantic", enqueue=False)
             for i, text in enumerate(["甲型材料的编号为 A1。", "乙型材料的编号为 B2。"])]
    fm.consolidate("s", "semantic", planner=lambda *args: {"actions": [{
        "action": "create", "memory_type": "semantic", "assertion_kind": "stated",
        "content": f["content"], "source_fact_ids": [f["id"]], "reason": "原文支持",
    } for f in facts]})
    rows = fm.query_hits("s", "甲型材料编号", limit=2)
    assert len({row["content"] for row in rows}) == 2


def test_new_read_adapter_semantically_recalls_existing_typed_memory_without_rewriting(monkeypatch):
    from memory import service, repository, fact_retrieval
    from memory.models import MemoryCandidate
    monkeypatch.setattr(settings, "FACT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "FACT_MEMORY_SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "PREFERENCE_CARDS_ENABLED", False)
    monkeypatch.setattr(service, "search_memories", lambda *args, **kwargs: [])
    monkeypatch.setattr(fact_retrieval, "embed_text", lambda text: [1., 0.])
    row = repository.insert_memory(
        "s", MemoryCandidate("preference", "低糖的食物", .8, .9), source_note_id="n1")
    before = row.to_dict()
    hits = service.memory_search("s", "甜度习惯", memory_type="preference", limit=1)
    assert [h["id"] for h in hits] == [row.id]
    assert repository.get_memory(row.id).to_dict() == before
