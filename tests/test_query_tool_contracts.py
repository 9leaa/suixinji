"""Public ReAct tool contract and compatibility tests."""

from __future__ import annotations

from datetime import datetime, timedelta

from agent import query_agent


def test_react_prompt_exposes_business_tools_only():
    prompt = query_agent.REACT_SYSTEM_PROMPT

    for signature in (
        "search_notes(query",
        "get_note(note_id",
        "get_related_notes(note_id",
        "search_memories(query",
        "search_tasks(query",
    ):
        assert signature in prompt

    for legacy_signature in (
        "filter_notes(type",
        "semantic_search(query",
        "list_recent(days",
        "follow_links(note_id",
        "memory_search(query",
        "task_status_search(query",
    ):
        assert legacy_signature not in prompt


def test_search_notes_combines_query_type_tag_and_time_filters(monkeypatch):
    now = datetime.now().astimezone()
    rows = [
        {
            "id": "n1",
            "type": "任务",
            "tags": ["提醒", "待办"],
            "time": now.isoformat(),
            "score": 0.9,
        },
        {
            "id": "n2",
            "type": "任务",
            "tags": ["待办"],
            "time": now.isoformat(),
            "score": 0.8,
        },
        {
            "id": "n3",
            "type": "任务",
            "tags": ["提醒"],
            "time": (now - timedelta(days=40)).isoformat(),
            "score": 0.7,
        },
    ]
    calls = []
    monkeypatch.setattr(
        query_agent,
        "semantic_search",
        lambda space_id, query, top_k, min_score: calls.append((space_id, query, top_k, min_score)) or rows,
    )

    result = query_agent.search_notes(
        "space-1",
        "答辩",
        note_type="任务",
        tags=["提醒"],
        days=30,
        limit=2,
        min_score=0.2,
    )

    assert [row["id"] for row in result] == ["n1"]
    assert calls == [("space-1", "答辩", 8, 0.2)]


def test_search_notes_normalizes_common_english_type_alias(monkeypatch):
    calls = []
    monkeypatch.setattr(
        query_agent,
        "filter_notes",
        lambda space_id, **kwargs: calls.append((space_id, kwargs)) or [],
    )

    query_agent.search_notes("space-1", note_type="task", limit=5)

    assert calls[0][0] == "space-1"
    assert calls[0][1]["note_type"] == "任务"


def test_search_memories_merges_explicit_non_task_types(monkeypatch):
    calls = []

    def fake_memory_search(space_id, query, **kwargs):
        memory_type = kwargs.get("memory_type")
        calls.append(memory_type)
        if memory_type == "preference":
            return [{"id": "p1", "memory_type": "preference", "score": 0.9}]
        if memory_type == "semantic":
            return [{"id": "s1", "memory_type": "semantic", "score": 0.8}]
        return []

    monkeypatch.setattr(query_agent, "_memory_search_compat", fake_memory_search)

    result = query_agent.search_memories(
        "space-1",
        "咖啡和居住地",
        memory_types=["semantic", "preference"],
        limit=5,
    )

    assert calls == ["semantic", "preference"]
    assert [row["id"] for row in result] == ["p1", "s1"]


def test_search_tasks_separates_current_and_timeline(monkeypatch):
    monkeypatch.setattr(
        query_agent,
        "task_status_search",
        lambda *args, **kwargs: [{"id": "task-current", "memory_type": "task", "task_status": "todo"}],
    )
    monkeypatch.setattr(
        query_agent,
        "memory_history",
        lambda *args, **kwargs: [
            {"id": "task-v1", "memory_type": "task", "history": True},
            {"id": "semantic-v1", "memory_type": "semantic", "history": True},
        ],
    )

    current = query_agent.search_tasks("space-1", "毕业论文", view="current")
    timeline = query_agent.search_tasks("space-1", "毕业论文", view="timeline")

    assert [row["id"] for row in current] == ["task-current"]
    assert [row["id"] for row in timeline] == ["task-v1"]


def test_execute_tool_accepts_new_task_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(
        query_agent,
        "search_tasks",
        lambda space_id, query, **kwargs: calls.append((space_id, query, kwargs)) or [],
    )

    query_agent._execute_tool(
        "space-1",
        "search_tasks",
        {"query": "毕业论文", "view": "timeline", "limit": 6},
    )

    assert calls[0][0:2] == ("space-1", "毕业论文")
    assert calls[0][2]["view"] == "timeline"
    assert calls[0][2]["limit"] == 6
