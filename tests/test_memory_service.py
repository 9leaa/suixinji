from memory import service
from memory.service import format_memory_consolidate, format_memory_correct, format_memory_forget, format_memory_search, process_note_memory
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


def test_process_note_memory_supersedes_repeated_dislike_preference():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢喝牛奶"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我讨厌喝牛奶"})
    process_note_memory({"id": "note-3", "space_id": "space-1", "text": "我讨厌喝牛奶"})

    active = list_memories("space-1", status="active", memory_type="preference")
    superseded = list_memories("space-1", status="superseded", memory_type="preference")
    results = format_memory_search("space-1", "我喜欢喝什么")

    assert len(active) == 1
    assert "讨厌喝牛奶" in active[0].content
    assert len(active[0].sources) == 2
    assert len(superseded) == 1
    assert "讨厌喝牛奶" not in results
    assert "喜欢喝牛奶" not in results


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

    conflicts = list_memories("space-1", status="pending_review")

    assert len(conflicts) == 1
    assert "办公室" in conflicts[0].content


def test_process_note_memory_updates_task_status_in_place():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "记得完善 README"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "完成 README"})

    active = list_memories("space-1", status="active", memory_type="task")

    assert len(active) == 1
    assert active[0].task_status == "done"
    assert active[0].current_version == 2
    assert len(active[0].sources) == 2


def test_process_note_memory_keeps_distinct_numbered_tasks_separate():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "记得处理批量任务T1"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "记得处理批量任务T10"})

    active = list_memories("space-1", status="active", memory_type="task")

    assert report["results"][0]["action"] == "insert"
    assert len(active) == 2


def test_process_note_memory_keeps_distinct_ticket_or_version_tasks_separate():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "记得处理工单PROJ-123"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "记得处理工单PROJ-124"})

    active = list_memories("space-1", status="active", memory_type="task")

    assert report["results"][0]["action"] == "insert"
    assert len(active) == 2


def test_process_note_memory_updates_status_for_the_same_numbered_task():
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "记得处理批量任务T1"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "完成批量任务T1"})

    active = list_memories("space-1", status="active", memory_type="task")

    assert len(active) == 1
    assert active[0].task_status == "done"
    assert len(active[0].sources) == 2


def test_preference_retrieval_keeps_exact_numbered_topic_in_a_large_similar_set():
    for index in range(12):
        process_note_memory({"id": f"note-{index}", "space_id": "space-1", "text": f"我喜欢喝饮品A{index}"})

    report = process_note_memory({"id": "note-change", "space_id": "space-1", "text": "我现在不喜欢喝饮品A0了"})
    memories = list_memories("space-1", status=None, memory_type="preference")

    original = next(memory for memory in memories if memory.content == "用户喜欢喝饮品A0")
    replacement = next(memory for memory in memories if memory.content == "用户现在不喜欢喝饮品A0了")
    assert report["results"][0]["action"] == "supersede"
    assert original.status == "superseded"
    assert replacement.status == "active"


def test_task_retrieval_updates_exact_numbered_task_in_a_large_similar_set():
    for index in range(12):
        process_note_memory({"id": f"note-{index}", "space_id": "space-1", "text": f"记得处理批量任务T{index}"})

    report = process_note_memory({"id": "note-done", "space_id": "space-1", "text": "完成批量任务T0"})
    active = list_memories("space-1", status="active", memory_type="task")

    updated = next(memory for memory in active if {source.note_id for source in memory.sources} == {"note-0", "note-done"})
    assert report["results"][0]["action"] == "update_task"
    assert len(active) == 12
    assert updated.task_status == "done"


def test_memory_commands_correct_forget_and_search():
    report = process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})
    memory_id = report["results"][0]["memory_id"]

    assert "已修正记忆" in format_memory_correct(memory_id, "用户住在上海")
    assert "上海" in format_memory_search("space-1", "住在哪")
    assert "已软删除记忆" in format_memory_forget(memory_id)
    assert "没有找到匹配" in format_memory_search("space-1", "上海")


def test_memory_correction_rejects_sensitive_credentials():
    report = process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})
    memory_id = report["results"][0]["memory_id"]

    message = format_memory_correct(memory_id, "密码: abc123456")

    assert "未写入" in message
    assert "北京" in list_memories("space-1")[0].content


def test_format_memory_consolidate_uses_idempotent_scheduler(monkeypatch):
    calls = []

    def fake_run_once(cadence, *, space_ids=None, today=None):
        calls.append((cadence, space_ids, today))
        status = "completed" if len(calls) == 1 else "skipped"
        return {"cadence": cadence, "results": [{"space_id": "space-1", "status": status}]}

    monkeypatch.setattr(service, "run_memory_consolidation_once", fake_run_once)

    first = format_memory_consolidate("space-1", "monthly")
    second = format_memory_consolidate("space-1", "monthly")

    assert "完成" in first
    assert "未重复运行" in second
    assert len(calls) == 2
    assert calls[0][0] == "monthly"
    assert calls[0][1] == ["space-1"]
