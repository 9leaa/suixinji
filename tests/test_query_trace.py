"""文件作用：查询 Trace、来源及展示裁剪。

项目关系：本文件依赖 `agent`、`agent.query_planner`、`memory.trace`；被 暂无静态导入方或仅作为入口脚本执行。
"""


import json

from agent import query_agent
from agent.query_planner import QueryPlan
from memory.trace import latest_trace


def test_sources_render_only_final_selected_evidence():
    """函数功能：`test_sources_render_only_final_selected_evidence` 负责验证 sources render only final selected evidence 场景，服务于本文件职责：查询 Trace、来源及展示裁剪。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    selected = [
        {
            "id": "mem-garden",
            "memory_type": "episodic",
            "content": "今天逛了植物园。",
            "sources": [{"note_id": "note-garden"}],
        },
        {
            "id": "mem-agent-resume",
            "memory_type": "task",
            "content": "修改 Agent 简历。",
            "sources": [{"note_id": "note-resume"}],
        },
    ]
    answer = query_agent._with_sources("已找到相关记录。", selected)

    assert answer.split("来源（最多展示 5 条记忆和 5 条笔记）：\n", 1)[1].splitlines() == [
        "- memory:mem-garden｜episodic｜sources=1",
        "- memory:mem-agent-resume｜task｜sources=1",
    ]


def test_sources_limit_memory_and_notes_independently():
    """函数功能：`test_sources_limit_memory_and_notes_independently` 负责验证 sources limit memory and notes independently 场景，服务于本文件职责：查询 Trace、来源及展示裁剪。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    memories = [
        {"id": f"mem-{index}", "memory_type": "fact", "sources": []}
        for index in range(6)
    ]
    notes = [
        {"id": f"note-{index}", "title": f"笔记 {index}", "ts": "2026-07-27"}
        for index in range(6)
    ]

    lines = query_agent._source_lines(memories + notes)

    assert len(lines) == 10
    assert sum(line.startswith("- memory:") for line in lines) == 5
    assert sum(line.startswith("- note:") for line in lines) == 5
    assert "mem-5" not in "\n".join(lines)
    assert "note-5" not in "\n".join(lines)


def test_complex_query_sources_follow_fused_final_evidence(monkeypatch):
    """函数功能：`test_complex_query_sources_follow_fused_final_evidence` 负责验证 complex query sources follow fused final evidence 场景，服务于本文件职责：查询 Trace、来源及展示裁剪。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    question = "我喜欢喝什么，我找什么工作，什么时候去的植物园？"
    preferences = [
        {
            "id": f"mem-preference-{index}",
            "memory_type": "preference",
            "content": f"偏好 {index}",
            "sources": [],
        }
        for index in range(5)
    ]
    selected = [
        {
            "id": "mem-garden",
            "memory_type": "episodic",
            "content": "今天逛了植物园。",
            "sources": [{"note_id": "note-garden"}],
        },
        {
            "id": "mem-agent-resume",
            "memory_type": "task",
            "content": "修改 Agent 简历。",
            "sources": [{"note_id": "note-resume"}],
        },
    ]
    plan = QueryPlan(
        complexity="complex",
        rewritten_query=question,
        retrieval_queries=("植物园和 Agent 简历",),
        use_query_rewrite=False,
        use_decomposition=True,
        use_step_back=False,
        routing_state="complex",
    )

    monkeypatch.setattr(query_agent, "provisional_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(query_agent, "_intent_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(query_agent, "_deterministic_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(query_agent, "build_query_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(query_agent.settings, "QUERY_DECOMPOSITION_ENABLED", True)
    monkeypatch.setattr(query_agent, "_fuse_memory_results", lambda *args, **kwargs: selected)
    monkeypatch.setattr(
        query_agent,
        "_run_tool",
        lambda space_id, action, args, **kwargs: preferences if args["query"] == question else selected,
    )
    monkeypatch.setattr(query_agent, "_complete_json_with_hooks", lambda *args, **kwargs: {"final_answer": "已找到三类信息。"})

    answer = query_agent.answer_question("space-1", question, max_steps=1)

    assert "memory:mem-garden｜episodic｜sources=1" in answer
    assert "memory:mem-agent-resume｜task｜sources=1" in answer
    assert "mem-preference-0" not in answer


def test_complex_query_augments_each_clause_with_note_evidence(monkeypatch):
    """函数功能：`test_complex_query_augments_each_clause_with_note_evidence` 负责验证 complex query augments each clause with note evidence 场景，服务于本文件职责：查询 Trace、来源及展示裁剪。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    question = "我喜欢喝什么？我什么时候去的植物园？"
    plan = QueryPlan(
        complexity="complex",
        rewritten_query=question,
        retrieval_queries=("我喜欢喝什么", "我什么时候去的植物园"),
        use_query_rewrite=False,
        use_decomposition=True,
        use_step_back=False,
        routing_state="complex",
    )
    memories = [
        {"id": "mem-drink", "memory_type": "preference", "content": "用户喜欢喝汽水", "sources": []},
        {"id": "mem-garden", "memory_type": "episodic", "content": "今天逛了植物园", "sources": []},
    ]
    notes = {
        "我喜欢喝什么": [{"id": "note-drink", "title": "饮料偏好", "ts": "2026-07-27"}],
        "我什么时候去的植物园": [{"id": "note-garden", "title": "植物园", "ts": "2026-07-27"}],
    }
    calls: list[tuple[str, str]] = []

    def fake_run_tool(space_id, action, args, **kwargs):
        """函数功能：`fake_run_tool` 负责运行 tool，服务于本文件职责：查询 Trace、来源及展示裁剪。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
            action: action 参数，由调用方传入。
            args: args 参数，由调用方传入。
            **kwargs: kwargs 参数，由调用方传入。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        calls.append((action, args["query"]))
        if action == "memory_note_fallback":
            return notes[args["query"]]
        return memories

    monkeypatch.setattr(query_agent, "provisional_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(query_agent, "_intent_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(query_agent, "_deterministic_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(query_agent, "build_query_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(query_agent, "_fuse_memory_results", lambda *args, **kwargs: memories)
    monkeypatch.setattr(query_agent, "_run_tool", fake_run_tool)
    monkeypatch.setattr(query_agent.settings, "COMPLEX_QUERY_NOTE_AUGMENT_ENABLED", True)
    monkeypatch.setattr(query_agent.settings, "COMPLEX_QUERY_NOTE_AUGMENT_LIMIT", 2)
    monkeypatch.setattr(
        query_agent,
        "_complete_json_with_hooks",
        lambda *args, **kwargs: {
            "final_answer": "你喜欢喝汽水，今天去了植物园。",
            "evidence_ids": ["mem-drink", "mem-garden", "note-drink", "note-garden"],
        },
    )

    answer = query_agent.answer_question("space-1", question, max_steps=1)

    assert ("memory_note_fallback", "我喜欢喝什么") in calls
    assert ("memory_note_fallback", "我什么时候去的植物园") in calls
    assert "memory:mem-garden｜episodic｜sources=0" in answer
    assert "note:note-garden｜植物园｜2026-07-27" in answer


def test_answer_question_writes_query_trace_with_safe_steps(monkeypatch):
    """函数功能：`test_answer_question_writes_query_trace_with_safe_steps` 负责验证 answer question writes query trace with safe steps 场景，服务于本文件职责：查询 Trace、来源及展示裁剪。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    decisions = iter(
        [
            {"thought": "查长期记忆", "action": "memory_search", "args": {"query": "我喜欢什么", "memory_type": "preference"}},
            {"final_answer": "你喜欢直接的评价。"},
        ]
    )
    monkeypatch.setattr(query_agent, "complete_json", lambda system_prompt, user_prompt: next(decisions))
    monkeypatch.setattr(
        query_agent,
        "memory_search",
        lambda space_id, query, memory_type=None, min_score=query_agent.DEFAULT_MEMORY_MIN_SCORE, limit=8: [
            {
                "id": "mem-1",
                "memory_type": "preference",
                "content": "用户喜欢直接的评价",
                "sources": [{"note_id": "note-1"}],
            }
        ],
    )

    answer = query_agent.answer_question("space-1", "我喜欢什么评价？", max_steps=2)
    trace = latest_trace()
    steps = [step["step"] for step in trace["steps"]]
    trace_text = json.dumps(trace, ensure_ascii=False)

    assert "来源（最多展示 5 条记忆和 5 条笔记）：" in answer
    assert trace["trace_type"] == "memory_query"
    assert "query_received" in steps
    assert "query_routed" in steps
    assert "memory_search" in steps
    assert "rerank" in steps
    assert "evidence_selected" in steps
    assert "answer_generated" in steps
    assert "answer_returned" in steps
    assert "我喜欢什么评价" not in trace_text


def test_answer_question_falls_back_when_react_llm_fails_after_prefetch(monkeypatch):
    """函数功能：`test_answer_question_falls_back_when_react_llm_fails_after_prefetch` 负责验证 answer question falls back when react llm fails after prefetch 场景，服务于本文件职责：查询 Trace、来源及展示裁剪。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    def fail_complete_json(system_prompt, user_prompt):
        """函数功能：`fail_complete_json` 负责完成 json，服务于本文件职责：查询 Trace、来源及展示裁剪。
        传参：
            system_prompt: system prompt 参数，由调用方传入。
            user_prompt: user prompt 参数，由调用方传入。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        raise RuntimeError("LLM returned no message content")

    monkeypatch.setattr(query_agent, "complete_json", fail_complete_json)
    monkeypatch.setattr(
        query_agent,
        "memory_search",
        lambda space_id, query, memory_type=None, min_score=query_agent.DEFAULT_MEMORY_MIN_SCORE, limit=8: [
            {
                "id": "mem-1",
                "memory_type": "preference",
                "content": "用户讨厌喝牛奶",
                "sources": [{"note_id": "note-1"}],
            }
        ],
    )

    answer = query_agent.answer_question("space-1", "我讨厌喝什么？", max_steps=2)
    trace = latest_trace()
    steps = [step["step"] for step in trace["steps"]]

    assert "用户讨厌喝牛奶" in answer
    assert "来源（最多展示 5 条记忆和 5 条笔记）：" in answer
    assert "answer_failed" not in steps
    assert "answer_returned" in steps
