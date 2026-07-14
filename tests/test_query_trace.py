import json

from agent import query_agent
from memory.trace import latest_trace


def test_answer_question_writes_query_trace_with_safe_steps(monkeypatch):
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

    assert "来源：" in answer
    assert trace["trace_type"] == "memory_query"
    assert "query_received" in steps
    assert "query_routed" in steps
    assert "memory_search" in steps
    assert "rerank" in steps
    assert "evidence_selected" in steps
    assert "answer_generated" in steps
    assert "answer_returned" in steps
    assert "我喜欢什么评价" not in trace_text
