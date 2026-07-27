from bot.feishu_bot import _handle_memory_command, _handle_trace_command
from memory.service import format_trace, process_note_memory


def test_memory_command_list_and_stats():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "记得完善 README"})

    assert "长期记忆" in _handle_memory_command("space-1", "/memory list")
    assert "记忆统计" in _handle_memory_command("space-1", "/memory stats")
    assert "动态用户画像" in _handle_memory_command("space-1", "/memory profile")
    assert "最近记忆审理" in _handle_memory_command("space-1", "/memory decisions")


def test_memory_command_search_usage():
    assert "用法" in _handle_memory_command("space-1", "/memory")
    assert "没有找到" in _handle_memory_command("space-1", "/memory search 不存在")


def test_trace_command_latest():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我正在学习 Agent"})

    assert "Trace" in _handle_trace_command("/trace latest")


def test_trace_formatter_shows_all_steps_and_candidate_summary():
    steps = [
        {
            "step": f"step-{index}",
            "status": "success",
            "duration_ms": index,
            "input_summary": {"index": index},
            "output_summary": {"result": index},
        }
        for index in range(13)
    ]
    steps.extend(
        [
            {
                "step": "candidate_extracted",
                "status": "success",
                "duration_ms": 0,
                "input_summary": {},
                "output_summary": {
                    "candidate_id": "cand-test",
                    "memory_type": "preference",
                    "importance": 0.8,
                    "confidence": 0.9,
                    "should_store": True,
                    "content_preview": "用户密码：very-secret-value",
                    "evidence_preview": "喜欢喝汽水",
                },
            },
            {
                "step": "relation_decided",
                "status": "success",
                "duration_ms": 3,
                "input_summary": {"candidate_id": "cand-test"},
                "output_summary": {"relation": "new", "action": "insert"},
            },
        ]
    )
    trace = {
        "trace_id": "trace-test",
        "trace_type": "memory_write",
        "space_id": "space-test",
        "status": "success",
        "started_at": "2026-07-27T00:00:00+08:00",
        "finished_at": "2026-07-27T00:00:01+08:00",
        "steps": steps,
    }

    rendered = format_trace(trace)

    assert "步骤（共 15）" in rendered
    assert "1. step-0" in rendered
    assert "13. step-12" in rendered
    assert "候选（1）" in rendered
    assert "action=insert" in rendered
    assert "[sensitive content redacted]" in rendered
    assert "very-secret-value" not in rendered
