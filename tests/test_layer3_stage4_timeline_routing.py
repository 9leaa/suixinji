from __future__ import annotations

from agent import query_agent


def test_stage4_current_focus_question_with_previous_marker_is_not_history():
    route = query_agent._deterministic_route("之前说的那个当前重点是什么来着？")

    assert route is not None
    assert route["action"] == "memory_search"
    assert route["args"]["memory_type"] == "semantic"


def test_stage4_history_synthesis_uses_memory_history():
    route = query_agent._deterministic_route("总结上下文工程实验从开始到完成的过程。")

    assert route is not None
    assert route["action"] == "memory_history"
    assert route["reason"] == "history_synthesis_timeline"


def test_stage4_memory_history_prefers_versioned_timeline(monkeypatch):
    class Record:
        def __init__(self, memory_id, versions):
            self.id = memory_id
            self.space_id = "space-1"
            self.versions = versions

        def to_dict(self):
            return {
                "id": self.id,
                "memory_type": "task",
                "memory_key": self.id,
                "sources": [],
                "versions": [{"id": f"{self.id}_v{i}", "version": i, "content": f"{self.id} version {i}"} for i in range(1, self.versions + 1)],
            }

    monkeypatch.setattr(
        query_agent,
        "_memory_search_compat",
        lambda *_args, **_kwargs: [
            {"id": "m6", "content": "上下文工程实验待处理", "score": 0.99},
            {"id": "m1", "content": "上下文工程实验已完成", "score": 0.90},
        ],
    )
    monkeypatch.setattr(
        query_agent,
        "get_memory",
        lambda memory_id: Record(memory_id, 1 if memory_id == "m6" else 3),
    )

    rows = query_agent.memory_history("space-1", "总结上下文工程实验从开始到完成的过程")

    assert rows
    assert {row["memory_id"] for row in rows} == {"m1"}
