from __future__ import annotations

from agent import query_agent
from agent import query_intent


def test_query_intent_routes_natural_task_status_question(monkeypatch):
    """验证“查询意图routesnatural任务状态question”场景的预期行为与回归边界。"""
    monkeypatch.setattr(
        query_intent,
        "complete_json",
        lambda **_: {
            "intent": "task_status",
            "entity": "随心记",
            "attribute": "大模型供应商",
            "topic": "更换供应商",
            "time_scope": "current",
            "confidence": 0.94,
        },
    )

    intent = query_intent.classify_query_intent("随心记的大模型供应商换得怎么样？")
    route = query_intent.route_for_intent(intent, "随心记的大模型供应商换得怎么样？", memory_min_score=0.45, note_min_score=0.2, top_k=5)

    assert intent is not None
    assert route is not None
    assert route["action"] == "memory_search"
    assert route["args"]["memory_type"] == "task"
    assert "更换供应商" in route["args"]["query"]
    assert route["fallback"]["action"] == "memory_note_fallback"


def test_query_agent_uses_intent_memory_first(monkeypatch):
    """验证“查询Agentuses意图记忆首个”场景的预期行为与回归边界。"""
    from core import settings

    monkeypatch.setattr(settings, "QUERY_INTENT_MODEL_ENABLED", True)
    monkeypatch.setattr(settings, "QUERY_MEMORY_BARRIER_ENABLED", False)
    monkeypatch.setattr(query_agent, "provisional_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        query_intent,
        "complete_json",
        lambda **_: {"intent": "task_status", "entity": None, "attribute": None, "topic": None, "time_scope": "current", "confidence": 0.9},
    )
    calls = []
    monkeypatch.setattr(
        query_agent,
        "memory_search",
        lambda space_id, query, **kwargs: calls.append(kwargs) or [{"id": "mem-1", "memory_type": "task", "content": "正在更换供应商", "sources": []}],
    )
    monkeypatch.setattr(query_agent, "_synthesize_answer", lambda *args, **kwargs: "正在更换供应商")

    answer = query_agent.answer_question("v3-space", "换得怎么样？")

    assert calls and calls[0]["memory_type"] == "task"
    assert "正在更换供应商" in answer


def test_general_intent_suppresses_legacy_short_note_only_route(monkeypatch):
    """验证“general意图suppresseslegacyshort笔记only路由”场景的预期行为与回归边界。"""
    monkeypatch.setattr(
        query_intent,
        "complete_json",
        lambda **_: {"intent": "general_search", "entity": None, "attribute": None, "topic": None, "time_scope": "all", "confidence": 0.8},
    )

    intent = query_intent.classify_query_intent("那件事呢？")
    route = query_intent.route_for_intent(intent, "那件事呢？", memory_min_score=0.45, note_min_score=0.2, top_k=5)

    assert route is not None
    assert route["action"] == "memory_prefetch"


def test_task_intent_splits_embedded_ascii_topic_for_retrieval():
    """验证“任务意图splitsembeddedasciitopicforretrieval”场景的预期行为与回归边界。"""
    intent = query_intent.QueryIntent(
        intent="task_status",
        entity="用户",
        attribute="RAG知识",
        topic="RAG知识",
        time_scope="current",
        confidence=0.9,
    )

    route = query_intent.route_for_intent(intent, "rag学到哪里了？", memory_min_score=0.45, note_min_score=0.2, top_k=5)

    assert route is not None
    assert "rag" in route["args"]["query"].casefold().split()


def test_generic_task_inventory_falls_back_to_task_notes():
    """验证“generic任务inventoryfallsback转换为任务笔记列表”场景的预期行为与回归边界。"""
    intent = query_intent.QueryIntent(
        intent="task_status",
        entity="用户",
        attribute=None,
        topic=None,
        time_scope="current",
        confidence=0.9,
    )

    route = query_intent.route_for_intent(intent, "我现在有哪些任务？", memory_min_score=0.45, note_min_score=0.2, top_k=5)

    assert route is not None
    assert route["fallback"] == {"action": "filter_notes", "args": {"type": "任务", "limit": 30}}
