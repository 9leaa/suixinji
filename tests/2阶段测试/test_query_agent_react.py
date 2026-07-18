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
    monkeypatch.setattr(query_agent, "complete_json", lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not call llm")))

    answer = query_agent.answer_question(SPACE_ID, "   ")

    assert "你想问什么" in answer


def test_answer_question_fast_routes_current_task_then_synthesizes_once(monkeypatch):
    prompts = []

    def fake_complete_json(system_prompt, user_prompt, model_role=None):
        prompts.append(json.loads(user_prompt))
        assert model_role == "balanced"
        return {"final_answer": "你现在有 1 个任务：测试任务。"}

    monkeypatch.setattr(query_agent, "complete_json", fake_complete_json)
    monkeypatch.setattr(query_agent, "load_index", lambda space_id: list(NOTES))

    answer = query_agent.answer_question(SPACE_ID, "我现在有哪些任务？", max_steps=2)

    assert answer.startswith("你现在有 1 个任务：测试任务。")
    assert "来源：" in answer
    assert "note:task-1" in answer
    assert len(prompts) == 1
    assert prompts[0]["observations"][0]["tool"] == "memory_search"
    assert prompts[0]["observations"][1]["tool"] == "filter_notes"
    assert prompts[0]["observations"][1]["result"][0]["id"] == "task-1"


def test_answer_question_defaults_to_semantic_search_when_llm_returns_no_action(monkeypatch):
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
