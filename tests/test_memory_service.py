"""文件作用：服务编排及命令输出。

项目关系：本文件依赖 `memory`、`memory.repository`、`memory.service`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from memory import service
from memory.service import format_memory_consolidate, format_memory_correct, format_memory_forget, format_memory_search, process_note_memory
from memory.repository import list_memories


def test_process_note_memory_merges_duplicate_sources():
    """函数功能：`test_process_note_memory_merges_duplicate_sources` 负责验证 process note memory merges duplicate sources 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢咖啡"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我喜欢咖啡"})

    memories = list_memories("space-1")
    assert len(memories) == 1
    assert len(memories[0].sources) == 2


def test_process_note_memory_supersedes_changed_preference():
    """函数功能：`test_process_note_memory_supersedes_changed_preference` 负责验证 process note memory supersedes changed preference 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢咖啡"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我暂时不喝咖啡"})

    active = list_memories("space-1", status="active")
    superseded = list_memories("space-1", status="superseded")

    assert len(active) == 1
    assert "暂时不喝咖啡" in active[0].content
    assert len(superseded) == 1


def test_process_note_memory_supersedes_repeated_dislike_preference():
    """函数功能：`test_process_note_memory_supersedes_repeated_dislike_preference` 负责验证 process note memory supersedes repeated dislike preference 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`test_process_note_memory_supersedes_changed_city` 负责验证 process note memory supersedes changed city 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我搬到上海了"})

    active = list_memories("space-1", status="active")
    superseded = list_memories("space-1", status="superseded")

    assert len(active) == 1
    assert "上海" in active[0].content
    assert len(superseded) == 1


def test_process_note_memory_preserves_ambiguous_preference_conflict():
    """函数功能：`test_process_note_memory_preserves_ambiguous_preference_conflict` 负责验证 process note memory preserves ambiguous preference conflict 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我喜欢远程工作"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "我更喜欢去办公室工作"})

    conflicts = list_memories("space-1", status="pending_review")

    assert len(conflicts) == 1
    assert "办公室" in conflicts[0].content


def test_process_note_memory_updates_task_status_in_place():
    """函数功能：`test_process_note_memory_updates_task_status_in_place` 负责验证 process note memory updates task status in place 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "记得完善 README"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "完成 README"})

    active = list_memories("space-1", status="active", memory_type="task")

    assert len(active) == 1
    assert active[0].task_status == "done"
    assert active[0].current_version == 2
    assert len(active[0].sources) == 2


def test_process_note_memory_keeps_distinct_numbered_tasks_separate():
    """函数功能：`test_process_note_memory_keeps_distinct_numbered_tasks_separate` 负责验证 process note memory keeps distinct numbered tasks separate 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "记得处理批量任务T1"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "记得处理批量任务T10"})

    active = list_memories("space-1", status="active", memory_type="task")

    assert report["results"][0]["action"] == "insert"
    assert len(active) == 2


def test_process_note_memory_keeps_distinct_ticket_or_version_tasks_separate():
    """函数功能：`test_process_note_memory_keeps_distinct_ticket_or_version_tasks_separate` 负责验证 process note memory keeps distinct ticket or version tasks separate 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "记得处理工单PROJ-123"})
    report = process_note_memory({"id": "note-2", "space_id": "space-1", "text": "记得处理工单PROJ-124"})

    active = list_memories("space-1", status="active", memory_type="task")

    assert report["results"][0]["action"] == "insert"
    assert len(active) == 2


def test_process_note_memory_updates_status_for_the_same_numbered_task():
    """函数功能：`test_process_note_memory_updates_status_for_the_same_numbered_task` 负责验证 process note memory updates status for the same numbered task 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-1", "space_id": "space-1", "text": "记得处理批量任务T1"})
    process_note_memory({"id": "note-2", "space_id": "space-1", "text": "完成批量任务T1"})

    active = list_memories("space-1", status="active", memory_type="task")

    assert len(active) == 1
    assert active[0].task_status == "done"
    assert len(active[0].sources) == 2


def test_preference_retrieval_keeps_exact_numbered_topic_in_a_large_similar_set():
    """函数功能：`test_preference_retrieval_keeps_exact_numbered_topic_in_a_large_similar_set` 负责验证 preference retrieval keeps exact numbered topic in a large similar set 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`test_task_retrieval_updates_exact_numbered_task_in_a_large_similar_set` 负责验证 task retrieval updates exact numbered task in a large similar set 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    for index in range(12):
        process_note_memory({"id": f"note-{index}", "space_id": "space-1", "text": f"记得处理批量任务T{index}"})

    report = process_note_memory({"id": "note-done", "space_id": "space-1", "text": "完成批量任务T0"})
    active = list_memories("space-1", status="active", memory_type="task")

    updated = next(memory for memory in active if {source.note_id for source in memory.sources} == {"note-0", "note-done"})
    assert report["results"][0]["action"] == "update_task"
    assert len(active) == 12
    assert updated.task_status == "done"


def test_memory_commands_correct_forget_and_search():
    """函数功能：`test_memory_commands_correct_forget_and_search` 负责验证 memory commands correct forget and search 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    report = process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})
    memory_id = report["results"][0]["memory_id"]

    assert "已修正记忆" in format_memory_correct(memory_id, "用户住在上海")
    assert "上海" in format_memory_search("space-1", "住在哪")
    assert "已软删除记忆" in format_memory_forget(memory_id)
    assert "没有找到匹配" in format_memory_search("space-1", "上海")


def test_memory_correction_rejects_sensitive_credentials():
    """函数功能：`test_memory_correction_rejects_sensitive_credentials` 负责验证 memory correction rejects sensitive credentials 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    report = process_note_memory({"id": "note-1", "space_id": "space-1", "text": "我住在北京"})
    memory_id = report["results"][0]["memory_id"]

    message = format_memory_correct(memory_id, "密码: abc123456")

    assert "未写入" in message
    assert "北京" in list_memories("space-1")[0].content


def test_format_memory_consolidate_uses_idempotent_scheduler(monkeypatch):
    """函数功能：`test_format_memory_consolidate_uses_idempotent_scheduler` 负责验证 format memory consolidate uses idempotent scheduler 场景，服务于本文件职责：服务编排及命令输出。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    calls = []

    def fake_run_once(cadence, *, space_ids=None, today=None):
        """函数功能：`fake_run_once` 负责运行 once，服务于本文件职责：服务编排及命令输出。
        传参：
            cadence: cadence 参数，由调用方传入。
            space_ids: space ids 参数，由调用方传入，默认值为 `None`。
            today: today 参数，由调用方传入，默认值为 `None`。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
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
