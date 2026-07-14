import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from memory.models import MemoryCandidate
from memory.repository import _run_write, add_source, get_memory, insert_memory, list_memories, update_memory


def test_run_write_retries_locked_errors():
    attempts = {"count": 0}

    def operation():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert _run_write(operation, max_attempts=3) == "ok"
    assert attempts["count"] == 3


def test_run_write_does_not_retry_non_locked_errors():
    attempts = {"count": 0}

    def operation():
        attempts["count"] += 1
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        _run_write(operation, max_attempts=3)
    assert attempts["count"] == 1


def test_concurrent_memory_writes_keep_sources_and_versions():
    def write(idx: int) -> str:
        memory = insert_memory(
            f"space-{idx % 4}",
            MemoryCandidate("semantic", f"用户正在测试并发写入 {idx}", 0.8, 0.9),
            source_note_id=f"note-{idx}",
        )
        add_source(memory.id, f"note-extra-{idx}", "supported_by")
        updated = update_memory(memory.id, content=f"用户正在测试并发写入 {idx} updated", reason="concurrency_test")
        assert updated is not None
        return memory.id

    with ThreadPoolExecutor(max_workers=8) as pool:
        memory_ids = list(pool.map(write, range(24)))

    assert len(memory_ids) == 24
    assert sum(len(list_memories(f"space-{idx}", status=None, limit=100)) for idx in range(4)) == 24
    sample = get_memory(memory_ids[0])
    assert sample is not None
    assert len(sample.sources) == 2
    assert sample.current_version == 2
