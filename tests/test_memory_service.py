from memory.service import format_memory_correct, format_memory_forget, format_memory_search, process_note_memory
from memory.repository import list_memories


def test_process_note_memory_merges_duplicate_sources():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢咖啡"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我喜欢咖啡"})

    memories = list_memories("space-1")
    assert len(memories) == 1
    assert len(memories[0].sources) == 2


def test_process_note_memory_supersedes_changed_preference():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢咖啡"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我暂时不喝咖啡"})

    active = list_memories("space-1", status="active")
    superseded = list_memories("space-1", status="superseded")

    assert len(active) == 1
    assert "暂时不喝咖啡" in active[0].content
    assert len(superseded) == 1


def test_process_note_memory_supersedes_changed_city():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我搬到上海了"})

    active = list_memories("space-1", status="active")
    superseded = list_memories("space-1", status="superseded")

    assert len(active) == 1
    assert "上海" in active[0].content
    assert len(superseded) == 1


def test_process_note_memory_preserves_ambiguous_preference_conflict():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢远程工作"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我更喜欢去办公室工作"})

    conflicts = list_memories("space-1", status="conflicted")

    assert len(conflicts) == 2
    assert any(source.relation == "contradicted_by" for memory in conflicts for source in memory.sources)


def test_process_note_memory_updates_task_status_in_place():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "记得完善 README"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "完成 README"})

    active = list_memories("space-1", status="active", memory_type="task")

    assert len(active) == 1
    assert active[0].task_status == "done"
    assert active[0].current_version == 2
    assert len(active[0].sources) == 2


def test_memory_commands_correct_forget_and_search():
    report = process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})
    memory_id = report["results"][0]["memory_id"]

    assert "已修正记忆" in format_memory_correct(memory_id, "用户住在上海")
    assert "上海" in format_memory_search("space-1", "住在哪")
    assert "已软删除记忆" in format_memory_forget(memory_id)
    assert "没有找到匹配" in format_memory_search("space-1", "上海")
