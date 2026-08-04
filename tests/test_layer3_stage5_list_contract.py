from __future__ import annotations

from types import SimpleNamespace

from agent import query_agent


def _record(**fields):
    return SimpleNamespace(to_dict=lambda: dict(fields))


def test_stage5_route_parses_three_item_limit():
    route = query_agent._deterministic_route("列出我当前三个项目的状态。")

    assert route is not None
    assert route["action"] == "list_tasks"
    assert route["args"]["limit"] == 3


def test_stage5_list_tasks_prefers_complete_current_items_and_dedupes(monkeypatch):
    monkeypatch.setattr(
        query_agent,
        "list_memories",
        lambda *_args, **_kwargs: [
            _record(id="m6", memory_type="task", scope={"canonical_topic": "随心记评测"}, object_value="随心记评测", task_status="todo", current_value=None, content="随心记评测待处理", sources=[{"note_id": "s6"}], updated_at="2026-08-02T00:00:00Z"),
            _record(id="m4", memory_type="task", canonical_topic="论文发言稿", task_status="todo", current_value=None, content="论文发言稿待处理", sources=[{"note_id": "s4"}], updated_at="2026-08-02T00:00:00Z"),
            _record(id="m1", memory_type="task", canonical_topic="随心记评测", task_status="todo", current_value=None, content="随心记评测:todo", sources=[{"note_id": "s1"}], updated_at="2026-08-02T00:00:00Z"),
            _record(id="m5", memory_type="task", canonical_topic="思维导图连线修复", task_status="todo", current_value=None, content="思维导图连线修复待处理", sources=[{"note_id": "s5"}], updated_at="2026-08-02T00:00:00Z"),
            _record(id="m2", memory_type="task", canonical_topic="检索质量优化", task_status="blocked", current_value=None, content="检索质量优化:blocked", sources=[{"note_id": "s2"}], updated_at="2026-08-02T00:00:00Z"),
            _record(id="m3", memory_type="task", canonical_topic="上下文工程实验", task_status="done", current_value=None, content="上下文工程实验:done", sources=[{"note_id": "s3"}], updated_at="2026-08-02T00:00:00Z"),
        ],
    )

    rows = query_agent.list_tasks("space-1", limit=3)

    assert [row["id"] for row in rows] == ["m1", "m2", "m3"]


def test_stage5_list_recent_episodes_orders_by_business_time(monkeypatch):
    monkeypatch.setattr(
        query_agent,
        "list_memories",
        lambda *_args, **_kwargs: [
            _record(id="m1", memory_type="episodic", canonical_topic="提交论文初稿", content="用户在2026-07-27提交论文初稿", sources=[{"note_id": "s1", "observed_at": "2026-07-27T00:00:00Z"}], valid_from="2026-08-02T00:00:00Z", updated_at="2026-08-02T00:00:00Z"),
            _record(id="m2", memory_type="episodic", canonical_topic="完成体检", content="用户在2026-07-20完成体检", sources=[{"note_id": "s2", "observed_at": "2026-07-20T00:00:00Z"}], valid_from="2026-08-02T00:00:00Z", updated_at="2026-08-02T00:00:00Z"),
        ],
    )

    rows = query_agent.list_recent_episodes("space-1", limit=2)

    assert [row["id"] for row in rows] == ["m1", "m2"]


def test_stage5_parser_and_profile_slots_are_structured(monkeypatch):
    constraints = query_agent.parse_list_constraints("列出当前两个项目的状态")
    assert constraints == {"limit": 2, "memory_type": "task", "status": None, "recent": False}

    rows = {"slots": {"task": [{"id": "m1", "memory_type": "task", "content": "项目:todo"}]}}
    assert query_agent._result_ids(rows) == ["m1"]
    bundle = query_agent._build_evidence_bundle([], [{"tool": "profile_summary", "result": rows}])
    assert [item.id for item in bundle.items] == ["m1"]
