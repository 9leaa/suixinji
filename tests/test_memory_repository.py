from memory.models import MemoryCandidate
from memory.repository import (
    add_source,
    correct_memory,
    get_memory,
    insert_memory,
    list_memories,
    purge_memory,
    schema_tables,
    search_memories,
    soft_delete_memory,
    stats,
)


def test_insert_memory_preserves_source_and_initial_version():
    assert {"memories", "memory_sources", "memory_versions", "memory_vectors"}.issubset(schema_tables())

    candidate = MemoryCandidate(
        memory_type="semantic",
        content="用户正在学习 Agent 工程开发",
        importance=0.8,
        confidence=0.9,
        entities=["Agent"],
    )

    memory = insert_memory("space-1", candidate, source_note_id="note-1")

    assert memory.id.startswith("mem_")
    assert memory.status == "active"
    assert memory.current_version == 1
    assert memory.sources[0].note_id == "note-1"
    assert memory.sources[0].relation == "created_from"
    assert memory.versions[0].content == "用户正在学习 Agent 工程开发"


def test_add_source_is_idempotent():
    memory = insert_memory("space-1", MemoryCandidate("preference", "用户喜欢咖啡", 0.7, 0.8), source_note_id="note-1")

    assert add_source(memory.id, "note-2", "supported_by") is True
    assert add_source(memory.id, "note-2", "supported_by") is False

    loaded = get_memory(memory.id)
    assert loaded is not None
    assert len(loaded.sources) == 2


def test_correct_and_soft_delete_create_versions_and_hide_from_active_search():
    memory = insert_memory("space-1", MemoryCandidate("preference", "用户喜欢苹果", 0.7, 0.8), source_note_id="note-1")

    corrected = correct_memory(memory.id, "用户对苹果过敏")
    assert corrected is not None
    assert corrected.current_version == 2
    assert corrected.content == "用户对苹果过敏"

    deleted = soft_delete_memory(memory.id)
    assert deleted is not None
    assert deleted.status == "deleted"
    assert deleted.current_version == 3

    assert search_memories("space-1", "苹果") == []


def test_expired_memory_is_hidden_from_default_search():
    memory = insert_memory("space-1", MemoryCandidate("semantic", "用户住在上海", 0.8, 0.9), source_note_id="note-1")
    from memory.lifecycle import expire

    assert expire(memory.id) is True
    assert search_memories("space-1", "上海") == []
    assert search_memories("space-1", "上海", include_inactive=True, min_score=0.1)


def test_search_memories_respects_min_score():
    insert_memory("space-1", MemoryCandidate("preference", "用户喜欢咖啡", 0.8, 0.9), source_note_id="note-1")

    assert search_memories("space-1", "喜欢", min_score=0.1)
    assert search_memories("space-1", "喜欢", min_score=0.95) == []
    assert search_memories("space-1", "火星基地", min_score=0.1) == []


def test_purge_memory_removes_record_and_audit_rows():
    memory = insert_memory("space-1", MemoryCandidate("preference", "用户喜欢苹果", 0.7, 0.8), source_note_id="note-1")
    assert purge_memory(memory.id) is True
    assert get_memory(memory.id) is None
    assert purge_memory(memory.id) is False


def test_list_and_stats_by_space():
    insert_memory("space-1", MemoryCandidate("task", "完善 README", 0.8, 0.9), source_note_id="note-1")
    insert_memory("space-2", MemoryCandidate("semantic", "用户住在上海", 0.8, 0.9), source_note_id="note-2")

    memories = list_memories("space-1")
    assert len(memories) == 1
    assert memories[0].memory_type == "task"
    assert stats("space-1")["by_type"] == {"task": 1}
