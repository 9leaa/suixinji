from __future__ import annotations

from agent import query_agent


def test_mixed_language_current_focus_rewrite_is_generic(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_MIXED_LANGUAGE_REWRITE_ENABLED", True)

    assert query_agent._mixed_language_query_rewrites("What am I mainly working on now?") == ["我现在主要在做什么？"]
    assert query_agent._mixed_language_query_rewrites("What is my current focus?") == ["我现在主要在做什么？"]


def test_mixed_language_rewrite_keeps_current_semantic_evidence(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_MIXED_LANGUAGE_REWRITE_ENABLED", True)
    rewritten = query_agent._mixed_language_query_rewrites("What am I mainly working on now?")[0]
    evidence = query_agent._relevant_evidence_items(
        rewritten,
        [{"id": "focus", "memory_type": "semantic", "content": "用户当前重点是项目规划", "score": 0.48}],
    )

    assert [item["id"] for item in evidence] == ["focus"]


def test_mixed_language_rewrite_can_be_disabled(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_MIXED_LANGUAGE_REWRITE_ENABLED", False)

    assert query_agent._mixed_language_query_rewrites("What am I mainly working on now?") == []
