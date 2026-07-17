from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, inspect, select

from core.wal import WalRecord
from infrastructure.database import get_engine, session_scope
from infrastructure.schema import Delivery, InboxMessage, MemoryVersion, Space
from memory.models import MemoryCandidate
from repositories.postgres import delivery, inbox, memory, notes, summary, tasks, vectors
from storage.note_storage import NoteMetadata
from storage.vector_store import VectorItem


def _postgres_ready() -> bool:
    if not os.getenv("DATABASE_URL"):
        return False
    try:
        return "spaces" in inspect(get_engine()).get_table_names()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _postgres_ready(), reason="PostgreSQL integration database is not configured")


@pytest.fixture
def pg_space():
    space_id = f"test-{uuid.uuid4().hex}"
    yield space_id
    with session_scope() as session:
        session.execute(delete(Space).where(Space.id == space_id))


def _wal(space_id: str, message_id: str) -> WalRecord:
    return WalRecord(
        id=f"wal-{uuid.uuid4().hex}",
        source="test",
        event_id=None,
        message_id=message_id,
        space_id=space_id,
        chat_id="chat-test",
        chat_type="p2p",
        sender={},
        ts="2026-07-17T12:00:00+08:00",
        text="repository contract",
    )


def test_postgres_inbox_and_note_contract(pg_space):
    message_id = f"message-{uuid.uuid4().hex}"
    record = _wal(pg_space, message_id)
    assert inbox.append_message_once(record) is True
    assert inbox.append_message_once(record) is False
    assert [item["message_id"] for item in inbox.load_pending_records(pg_space)] == [message_id]
    inbox.mark_processed(pg_space, record.id)
    assert inbox.load_pending_records(pg_space) == []

    note = NoteMetadata(
        id=record.id,
        message_id=message_id,
        space_id=pg_space,
        ts=record.ts,
        title="Contract",
        tags=["postgres", "contract"],
        type="学习",
        summary="Repository contract",
        text=record.text,
        related=[],
    )
    assert notes.save_note(note) is True
    assert notes.save_note(note) is False
    loaded = notes.find_note(pg_space, note.id)
    assert loaded is not None
    assert loaded["tags"] == ["contract", "postgres"] or loaded["tags"] == ["postgres", "contract"]
    assert notes.update_note_metadata(pg_space, note.id, summary="updated")["summary"] == "updated"


def test_postgres_pgvector_contract(pg_space):
    note = NoteMetadata(
        id=f"note-{uuid.uuid4().hex}",
        message_id=f"message-{uuid.uuid4().hex}",
        space_id=pg_space,
        ts="2026-07-17T12:00:00+08:00",
        title="Vector",
        tags=[],
        type="学习",
        summary="Vector contract",
        text="vector contract",
        related=[],
    )
    assert notes.save_note(note) is True
    embedding = [0.0] * 1024
    embedding[0] = 1.0
    item = VectorItem(note.id, note.message_id, note.text, embedding, {"message_id": note.message_id, "embedding_model": "contract"})
    assert vectors.add_vector_item(pg_space, item) is True
    assert vectors.add_vector_item(pg_space, item) is False
    results = vectors.search_related(pg_space, embedding, top_k=1, min_score=0.99)
    assert len(results) == 1
    assert results[0].note_id == note.id


def test_postgres_memory_summary_and_delivery_contract(pg_space):
    created = memory.insert_memory(pg_space, MemoryCandidate("preference", "用户喜欢咖啡", 0.8, 0.9), source_note_id="note-1")
    corrected = memory.correct_memory(created.id, "用户喜欢牛奶")
    assert corrected is not None
    assert corrected.current_version == 2
    assert memory.search_memories(pg_space, "牛奶", min_score=0.1)

    sub = summary.enable_summary_subscription(pg_space, "chat-test")
    assert sub.enabled is True
    assert summary.update_summary_time(pg_space, "chat-test", "21:30").time == "21:30"

    key = f"delivery-{uuid.uuid4().hex}"
    assert delivery.reserve_delivery(key, delivery_type="query", space_id=pg_space) is not None
    assert delivery.reserve_delivery(key, delivery_type="query", space_id=pg_space) is None
    delivery.mark_sent(key)
    assert delivery.get_delivery(key).status == "sent"

    task_id = f"task-{uuid.uuid4().hex}"
    task = {
        "id": task_id,
        "task_type": "ingest",
        "space_id": pg_space,
        "idempotency_key": f"ingest:{pg_space}:contract",
        "payload": {"source": "contract"},
    }
    assert tasks.create_task(task) is True
    assert tasks.create_task(task) is False
    tasks.update_task_status(task_id, "running", attempt_count=1)
    assert tasks.get_task(task_id)["status"] == "running"


def test_postgres_same_message_is_inserted_once_under_concurrency(pg_space):
    message_id = f"message-{uuid.uuid4().hex}"

    def write(index: int) -> bool:
        return inbox.append_message_once(_wal(pg_space, message_id))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(8)))
    assert results.count(True) == 1
    with session_scope() as session:
        count = session.execute(
            select(InboxMessage).where(InboxMessage.space_id == pg_space, InboxMessage.source_message_id == message_id)
        ).scalars().all()
        assert len(count) == 1


def test_postgres_memory_versions_are_serialized_under_concurrency(pg_space):
    created = memory.insert_memory(pg_space, MemoryCandidate("semantic", "版本 0", 0.8, 0.9), source_note_id="note-0")

    def update(index: int):
        return memory.update_memory(created.id, content=f"版本 {index + 1}", reason="concurrency-test")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(update, range(6)))
    loaded = memory.get_memory(created.id)
    assert loaded is not None
    assert loaded.current_version == 7
    with session_scope() as session:
        versions = list(session.execute(
            select(MemoryVersion.version).where(MemoryVersion.memory_id == created.id).order_by(MemoryVersion.version)
        ).scalars())
    assert versions == list(range(1, 8))


def test_postgres_delivery_reservation_is_single_winner(pg_space):
    key = f"delivery-{uuid.uuid4().hex}"

    def reserve(_index: int):
        return delivery.reserve_delivery(key, delivery_type="query", space_id=pg_space)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(item is not None for item in results) == 1
    with session_scope() as session:
        assert session.execute(select(Delivery).where(Delivery.delivery_key == key)).scalar_one().attempt_count == 1
