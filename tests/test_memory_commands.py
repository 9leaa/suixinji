from bot.feishu_bot import _handle_memory_command, _handle_trace_command
from memory.service import process_note_memory


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
