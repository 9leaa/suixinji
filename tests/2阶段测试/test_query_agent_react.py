"""文件作用：早期 ReAct 查询流程。

项目关系：本文件依赖 `agent`；被 暂无静态导入方或仅作为入口脚本执行。
"""


import json

from agent import query_agent

SPACE_ID = "space-test"

NOTES = [
    {
        "id": "task-1",
        "message_id": "m1",
        "space_id": SPACE_ID,
        "ts": "2026-06-07T12:00:00+08:00",
        "title": "测试任务",
        "type": "任务",
        "tags": ["待办", "提醒"],
        "summary": "需要测试任务查询。",
        "text": "记得测试任务查询。",
        "related": [],
    }
]


def test_answer_question_empty_question_does_not_call_llm(monkeypatch):
    """函数功能：`test_answer_question_empty_question_does_not_call_llm` 负责验证 answer question empty question does not call llm 场景，服务于本文件职责：早期 ReAct 查询流程。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(query_agent, "complete_json", lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not call llm")))

    answer = query_agent.answer_question(SPACE_ID, "   ")

    assert "你想问什么" in answer


def test_answer_question_fast_routes_current_task_then_synthesizes_once(monkeypatch):
    """函数功能：`test_answer_question_fast_routes_current_task_then_synthesizes_once` 负责验证 answer question fast routes current task then synthesizes once 场景，服务于本文件职责：早期 ReAct 查询流程。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    prompts = []

    def fake_complete_json(system_prompt, user_prompt, model_role=None):
        """函数功能：`fake_complete_json` 负责完成 json，服务于本文件职责：早期 ReAct 查询流程。
        传参：
            system_prompt: system prompt 参数，由调用方传入。
            user_prompt: user prompt 参数，由调用方传入。
            model_role: model role 参数，由调用方传入，默认值为 `None`。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        prompts.append(json.loads(user_prompt))
        assert model_role == "balanced"
        return {"final_answer": "你现在有 1 个任务：测试任务。"}

    monkeypatch.setattr(query_agent, "complete_json", fake_complete_json)
    monkeypatch.setattr(query_agent, "load_index", lambda space_id: list(NOTES))

    answer = query_agent.answer_question(SPACE_ID, "我现在有哪些任务？", max_steps=2)

    assert answer.startswith("你现在有 1 个任务：测试任务。")
    assert "来源（最多展示 5 条记忆和 5 条笔记）：" in answer
    assert "note:task-1" in answer
    assert len(prompts) == 1
    assert prompts[0]["observations"][0]["tool"] == "memory_search"
    assert prompts[0]["observations"][1]["tool"] == "filter_notes"
    assert prompts[0]["observations"][1]["result"][0]["id"] == "task-1"


def test_answer_question_defaults_to_semantic_search_when_llm_returns_no_action(monkeypatch):
    """函数功能：`test_answer_question_defaults_to_semantic_search_when_llm_returns_no_action` 负责验证 answer question defaults to semantic search when llm returns no action 场景，服务于本文件职责：早期 ReAct 查询流程。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    calls = []
    decisions = iter(
        [
            {"thought": "没有明确动作。"},
            {"final_answer": "找到语义结果。"},
        ]
    )

    monkeypatch.setattr(query_agent, "complete_json", lambda system_prompt, user_prompt, model_role=None: next(decisions))
    monkeypatch.setattr(
        query_agent,
        "semantic_search",
        lambda space_id, query, top_k, min_score: calls.append((space_id, query, top_k, min_score))
        or [{"id": "semantic-1", "title": "语义结果", "summary": "相关内容。"}],
    )

    answer = query_agent.answer_question(SPACE_ID, "上次说的总结功能是什么？", max_steps=1)

    assert answer.startswith("找到语义结果。")
    assert "note:semantic-1" in answer
    assert calls == [(SPACE_ID, "上次说的总结功能是什么？", 5, query_agent.DEFAULT_QUERY_MIN_SCORE)]


def test_synthesize_answer_falls_back_when_llm_fails():
    """函数功能：`test_synthesize_answer_falls_back_when_llm_fails` 负责验证 synthesize answer falls back when llm fails 场景，服务于本文件职责：早期 ReAct 查询流程。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    observations = [
        {
            "tool": "filter_notes",
            "result": [
                {
                    "id": "task-1",
                    "title": "测试任务",
                    "summary": "需要测试任务查询。",
                }
            ],
        }
    ]

    answer = query_agent._fallback_answer(observations)

    assert "测试任务" in answer
    assert "需要测试任务查询" in answer
