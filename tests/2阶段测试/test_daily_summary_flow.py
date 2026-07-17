import json
from datetime import datetime, timezone, timedelta

from summary import daily_summary

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 6, 7, 0, 0, tzinfo=TZ)
END = datetime(2026, 6, 8, 0, 0, tzinfo=TZ)

NOTES = [
    {
        "id": "n1",
        "ts": "2026-06-07T09:00:00+08:00",
        "title": "早餐",
        "type": "生活",
        "tags": ["饮食", "日常"],
        "summary": "吃了早餐。",
        "text": "早上吃了馅饼。",
    },
    {
        "id": "n2",
        "ts": "2026-06-07T12:00:00+08:00",
        "title": "测试任务",
        "type": "任务",
        "tags": ["待办", "提醒"],
        "summary": "需要测试 P4。",
        "text": "记得测试 P4 总结。",
    },
]


def patch_summary_io(monkeypatch, tmp_path, notes=None):
    monkeypatch.setattr(daily_summary, "build_time_range", lambda range_key: (START, END))
    monkeypatch.setattr(daily_summary, "load_notes_in_range", lambda space_id, start, end: list(notes if notes is not None else NOTES))
    monkeypatch.setattr(daily_summary, "note_dir", lambda space_id: tmp_path / space_id)


def test_generate_summary_uses_draft_then_reflection_and_saves_result(monkeypatch, tmp_path):
    patch_summary_io(monkeypatch, tmp_path)
    calls = []
    responses = iter(
        [
            {"summary_markdown": "草稿总结"},
            {"final_summary": "最终总结"},
        ]
    )

    def fake_complete_json(system_prompt, user_prompt):
        calls.append((system_prompt, json.loads(user_prompt)))
        return next(responses)

    monkeypatch.setattr(daily_summary, "complete_json", fake_complete_json)

    result = daily_summary.generate_summary("space-1", "today")

    assert result.markdown == "最终总结"
    assert result.note_count == 2
    assert len(calls) == 2
    assert calls[0][1]["stats"]["note_count"] == 2
    assert calls[1][1]["draft"] == "草稿总结"

    summary_path = tmp_path / "space-1" / "summaries" / "2026-06-07_2026-06-08_today.md"
    assert summary_path.read_text(encoding="utf-8").strip() == "最终总结"

    index_path = summary_path.parent / "index.json"
    index_items = json.loads(index_path.read_text(encoding="utf-8"))
    assert index_items[0]["range_key"] == "today"
    assert index_items[0]["note_count"] == 2


def test_generate_summary_falls_back_when_llm_raises(monkeypatch, tmp_path):
    patch_summary_io(monkeypatch, tmp_path)
    monkeypatch.setattr(daily_summary, "complete_json", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("llm failed")))

    result = daily_summary.generate_summary("space-1", "today")

    assert "今天随心记总结" in result.markdown
    assert "共记录 2 条笔记" in result.markdown
    assert "早餐" in result.markdown


def test_generate_summary_with_no_notes_skips_llm(monkeypatch, tmp_path):
    patch_summary_io(monkeypatch, tmp_path, notes=[])
    monkeypatch.setattr(daily_summary, "complete_json", lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not call llm")))

    result = daily_summary.generate_summary("space-1", "today")

    assert result.note_count == 0
    assert result.markdown == "今天没有记录到随心记笔记。"


def test_generate_summary_includes_memory_state_changes(monkeypatch, tmp_path):
    patch_summary_io(monkeypatch, tmp_path)
    memory_changes = [
        {
            "id": "mem-1",
            "memory_type": "task",
            "content": "完善 README",
            "status": "active",
            "task_status": "done",
            "updated_at": "2026-06-07T12:10:00+08:00",
            "sources": [{"note_id": "n2"}],
        }
    ]
    monkeypatch.setattr(daily_summary, "load_memory_changes", lambda space_id, start, end: memory_changes)
    calls = []
    responses = iter([{"summary_markdown": "草稿"}, {"final_summary": "含记忆状态的总结"}])

    def fake_complete_json(system_prompt, user_prompt):
        calls.append(json.loads(user_prompt))
        return next(responses)

    monkeypatch.setattr(daily_summary, "complete_json", fake_complete_json)

    result = daily_summary.generate_summary("space-1", "today")

    assert result.memory_count == 1
    assert calls[0]["memory_changes"][0]["task_status"] == "done"
    assert calls[1]["memory_changes"][0]["content"] == "完善 README"
