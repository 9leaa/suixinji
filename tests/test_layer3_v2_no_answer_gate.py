from __future__ import annotations

from agent import query_agent


def test_topic_gate_rejects_same_type_different_preference_attribute(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED", True)
    evidence = query_agent._relevant_evidence_items(
        "我最喜欢的电影是什么？",
        [{"id": "coffee", "memory_type": "preference", "content": "用户喜欢咖啡", "predicate": "对咖啡的偏好", "score": 0.98}],
    )

    assert evidence == []


def test_topic_gate_keeps_matching_preference_attribute(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED", True)
    evidence = query_agent._relevant_evidence_items(
        "我喜欢咖啡吗？",
        [{"id": "coffee", "memory_type": "preference", "content": "用户喜欢咖啡", "predicate": "对咖啡的偏好", "score": 0.98}],
    )

    assert [item["id"] for item in evidence] == ["coffee"]


def test_topic_gate_uses_canonical_topic_when_content_is_short(monkeypatch):
    monkeypatch.setattr(query_agent.settings, "QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED", True)
    item = {"id": "movie", "memory_type": "preference", "content": "喜欢", "scope": {"canonical_topic": "最喜欢的电影"}, "score": 0.98}

    assert query_agent._relevant_evidence_items("我最喜欢的电影是什么？", [item]) == [item]
