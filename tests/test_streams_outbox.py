from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, func, select, update

from apps import handlers
from infrastructure.database import session_scope
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import RedisKeys
from infrastructure.schema import InboxMessage, OutboxEvent, Space, Task
from repositories.postgres.dispatch import enqueue_task, get_space_progress, receive_command
from repositories.postgres.outbox import relay_outbox_batch
from repositories.postgres.tasks import (
    claim_task,
    complete_task,
    defer_task,
    enqueue_due_retries,
    fail_task,
    get_task,
)
from runtime.streams import StreamClient
from runtime.streams.worker import StreamWorker, TaskOutcome

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL") or not os.getenv("REDIS_URL"),
    reason="PostgreSQL and Redis integration URLs are required",
)


@pytest.fixture
def distributed_scope():
    suffix = uuid.uuid4().hex
    space_id = f"dist-{suffix}"
    source = f"test-{suffix}"
    keys = RedisKeys(env=f"test-{suffix}")
    client = get_redis()
    yield space_id, source, keys, client
    with session_scope() as session:
        task_ids = list(session.execute(select(Task.id).where(Task.space_id == space_id)).scalars())
        if task_ids:
            session.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id.in_(task_ids)))
        session.execute(delete(Space).where(Space.id == space_id))
    for key in client.scan_iter(match=f"{keys.prefix}:*"):
        client.delete(key)


def _receive(
    space_id: str,
    source: str,
    message_id: str,
    *,
    task_type: str = "ingest",
    payload: dict | None = None,
    max_attempts: int = 5,
):
    return receive_command(
        source=source,
        source_message_id=message_id,
        source_event_id=None,
        tenant_id="default",
        space_id=space_id,
        chat_id="chat-test",
        chat_type="p2p",
        sender={"user_id": "test-user"},
        text_value="test command",
        received_at="2026-07-17T12:00:00+08:00",
        task_type=task_type,
        task_payload=payload or {"chat_id": "chat-test"},
        max_attempts=max_attempts,
    )


def _event_ids(task_ids: list[str]) -> list[str]:
    with session_scope() as session:
        return list(session.execute(select(OutboxEvent.id).where(OutboxEvent.aggregate_id.in_(task_ids))).scalars())


def _outbox_count(task_id: str) -> int:
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count(OutboxEvent.id)).where(OutboxEvent.aggregate_id == task_id)
            ).scalar_one()
        )


def test_transactional_receiver_has_one_inbox_task_and_outbox(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _index: _receive(space_id, source, "same-message"), range(10)))
    assert sum(result.created for result in results) == 1
    with session_scope() as session:
        assert len(list(session.execute(select(InboxMessage).where(InboxMessage.space_id == space_id)).scalars())) == 1
        tasks = list(session.execute(select(Task).where(Task.space_id == space_id)).scalars())
        assert len(tasks) == 1
        assert tasks[0].status == "queued"
        assert session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == tasks[0].id)).scalar_one() is not None


def test_only_first_root_is_published_and_completion_releases_one_next(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    first = _receive(space_id, source, "ordered-1")
    second = _receive(space_id, source, "ordered-2")
    third = _receive(space_id, source, "ordered-3")

    with session_scope() as session:
        assert session.get(Task, first.task_id).status == "queued"
        assert session.get(Task, second.task_id).status == "blocked"
        assert session.get(Task, third.task_id).status == "blocked"
    assert _outbox_count(str(first.task_id)) == 1
    assert _outbox_count(str(second.task_id)) == 0
    assert _outbox_count(str(third.task_id)) == 0

    assert claim_task(str(first.task_id), "ordered-worker") is not None
    complete_task(str(first.task_id), release_inbox_id=first.inbox_id)

    with session_scope() as session:
        assert session.get(Task, first.task_id).status == "completed"
        assert session.get(Task, second.task_id).status == "queued"
        assert session.get(Task, third.task_id).status == "blocked"
    assert _outbox_count(str(second.task_id)) == 1
    assert _outbox_count(str(third.task_id)) == 0
    assert get_space_progress(space_id) == {
        "processed_sequence_no": 1,
        "memory_watermark": 1,
        "memory_gap_sequence_no": None,
    }


def test_concurrent_completion_cannot_publish_the_next_root_twice(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    first = _receive(space_id, source, "concurrent-1")
    second = _receive(space_id, source, "concurrent-2")
    third = _receive(space_id, source, "concurrent-3")
    assert claim_task(str(first.task_id), "concurrent-worker") is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _index: complete_task(str(first.task_id), release_inbox_id=first.inbox_id), range(2)))

    with session_scope() as session:
        assert session.get(Task, second.task_id).status == "queued"
        assert session.get(Task, third.task_id).status == "blocked"
    assert _outbox_count(str(second.task_id)) == 1
    assert _outbox_count(str(third.task_id)) == 0


def test_ingest_memory_barrier_blocks_query_but_not_enrichment(distributed_scope, monkeypatch):
    space_id, source, _keys, _client = distributed_scope
    first = _receive(space_id, source, "memory-1")
    second = _receive(
        space_id,
        source,
        "memory-2",
        task_type="query",
        payload={
            "chat_id": "chat-test",
            "question": "what do I like",
            "delivery_key": "query:memory-2",
        },
    )
    root = claim_task(str(first.task_id), "ingest-worker")
    assert root is not None
    note_id = f"note-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        handlers,
        "process_record",
        lambda _record, defer_memory, defer_wal_completion: {"id": note_id},
    )

    outcome = handlers.handle_ingest(root)
    assert isinstance(outcome, TaskOutcome)
    assert outcome.activate_task_id
    critical_task_id = str(outcome.activate_task_id)

    with session_scope() as session:
        critical = session.get(Task, critical_task_id)
        memory_tasks = list(
            session.execute(select(Task).where(Task.space_id == space_id, Task.task_type == "memory")).scalars()
        )
        enrich = session.execute(
            select(Task).where(Task.space_id == space_id, Task.task_type == "enrichment")
        ).scalar_one()
        assert [row.id for row in memory_tasks] == [critical_task_id]
        assert critical.status == "blocked"
        assert critical.payload_json["barrier_inbox_id"] == first.inbox_id
        assert enrich.status == "queued"
        assert session.get(Task, second.task_id).status == "blocked"
    assert get_space_progress(space_id)["memory_watermark"] == 0

    complete_task(str(first.task_id), activate_task_id=critical_task_id)
    with session_scope() as session:
        assert session.get(Task, critical_task_id).status == "queued"
        assert session.get(Task, second.task_id).status == "blocked"
    assert _outbox_count(critical_task_id) == 1
    assert get_space_progress(space_id)["memory_watermark"] == 0

    critical = claim_task(critical_task_id, "memory-worker")
    assert critical is not None
    complete_task(critical_task_id, release_inbox_id=str(critical["payload_json"]["barrier_inbox_id"]))

    with session_scope() as session:
        assert session.get(InboxMessage, first.inbox_id).status == "processed"
        assert session.get(Task, second.task_id).status == "queued"
        enrich = session.execute(
            select(Task).where(Task.space_id == space_id, Task.task_type == "enrichment")
        ).scalar_one()
        assert enrich.status == "queued"
    assert get_space_progress(space_id) == {
        "processed_sequence_no": 1,
        "memory_watermark": 1,
        "memory_gap_sequence_no": None,
    }


def test_terminal_critical_memory_failure_records_gap_and_releases_next(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    first = _receive(space_id, source, "gap-1")
    second = _receive(space_id, source, "gap-2", task_type="query")
    critical_task_id, created = enqueue_task(
        task_type="memory",
        tenant_id="default",
        space_id=space_id,
        source_message_id="gap-1",
        idempotency_key=f"memory:extract:gap-{uuid.uuid4().hex}",
        payload={
            "operation": "extract",
            "note_id": "gap-note",
            "barrier_inbox_id": first.inbox_id,
            "parent_task_id": str(first.task_id),
        },
        max_attempts=1,
        initial_status="blocked",
        publish=False,
    )
    assert created
    assert claim_task(str(first.task_id), "gap-ingest") is not None
    complete_task(str(first.task_id), activate_task_id=critical_task_id)
    assert claim_task(critical_task_id, "gap-memory") is not None

    assert fail_task(critical_task_id, "RuntimeError: extraction failed", retry_delay_seconds=0.1) == "dead_letter"

    with session_scope() as session:
        assert session.get(InboxMessage, first.inbox_id).status == "failed"
        assert session.get(Task, second.task_id).status == "queued"
        space = session.get(Space, space_id)
        assert space.metadata_json["last_memory_gap"]["inbox_id"] == first.inbox_id
    assert get_space_progress(space_id) == {
        "processed_sequence_no": 1,
        "memory_watermark": 0,
        "memory_gap_sequence_no": 1,
    }


def test_terminal_root_failure_cancels_blocked_barrier_and_releases_next(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    first = _receive(space_id, source, "root-gap-1", max_attempts=1)
    second = _receive(space_id, source, "root-gap-2")
    critical_task_id, _ = enqueue_task(
        task_type="memory",
        tenant_id="default",
        space_id=space_id,
        source_message_id="root-gap-1",
        idempotency_key=f"memory:extract:root-gap-{uuid.uuid4().hex}",
        payload={
            "operation": "extract",
            "note_id": "root-gap-note",
            "barrier_inbox_id": first.inbox_id,
            "parent_task_id": str(first.task_id),
        },
        initial_status="blocked",
        publish=False,
    )
    assert claim_task(str(first.task_id), "root-gap-worker") is not None

    assert fail_task(str(first.task_id), "RuntimeError: ingest failed", retry_delay_seconds=0.1) == "dead_letter"

    with session_scope() as session:
        assert session.get(Task, critical_task_id).status == "cancelled"
        assert session.get(Task, second.task_id).status == "queued"
        assert session.get(InboxMessage, first.inbox_id).status == "failed"


def test_defer_does_not_consume_failure_budget(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    result = _receive(space_id, source, "defer-message", max_attempts=1)
    assert claim_task(str(result.task_id), "defer-worker") is not None
    defer_task(str(result.task_id), "dependency busy", retry_delay_seconds=0.1)

    task = get_task(str(result.task_id))
    assert task is not None
    assert task["status"] == "retry"
    assert task["defer_count"] == 1
    assert task["failure_count"] == 0

    time.sleep(0.11)
    assert enqueue_due_retries(limit=1, task_ids=[str(result.task_id)]) == 1
    assert claim_task(str(result.task_id), "failure-worker") is not None
    assert fail_task(str(result.task_id), "RuntimeError: real failure", retry_delay_seconds=0.1) == "dead_letter"
    task = get_task(str(result.task_id))
    assert task["defer_count"] == 1
    assert task["failure_count"] == 1


def test_outbox_relay_duplicate_publish_keeps_one_task(distributed_scope):
    space_id, source, keys, redis = distributed_scope
    result = _receive(space_id, source, "relay-message")
    streams = StreamClient(redis, keys=keys)
    event_ids = _event_ids([str(result.task_id)])
    assert relay_outbox_batch(streams, limit=10, event_ids=event_ids) == {"published": 1, "failed": 0}
    messages = streams.read("ingest", "consumer-a", count=10, block_ms=10)
    assert len(messages) == 1
    assert messages[0].fields["task_id"] == result.task_id
    with session_scope() as session:
        session.execute(update(OutboxEvent).where(OutboxEvent.aggregate_id == result.task_id).values(published_at=None))
    assert relay_outbox_batch(streams, limit=10, event_ids=event_ids)["published"] == 1
    duplicate = streams.read("ingest", "consumer-a", count=10, block_ms=10)
    assert duplicate[0].fields["task_id"] == result.task_id
    with session_scope() as session:
        assert len(list(session.execute(select(Task).where(Task.space_id == space_id)).scalars())) == 1


def test_pending_message_can_be_reclaimed_after_worker_crash(distributed_scope):
    space_id, source, keys, redis = distributed_scope
    result = _receive(space_id, source, "crash-message")
    streams = StreamClient(redis, keys=keys)
    relay_outbox_batch(streams, limit=10, event_ids=_event_ids([str(result.task_id)]))
    claimed_by_a = streams.read("ingest", "worker-a", count=1, block_ms=10)
    assert len(claimed_by_a) == 1
    time.sleep(0.02)
    reclaimed = streams.reclaim("ingest", "worker-b", min_idle_ms=1, count=10)
    assert reclaimed
    assert reclaimed[0].message_id == claimed_by_a[0].message_id
    streams.ack("ingest", reclaimed[0].message_id)


def test_stream_worker_executes_business_once_after_duplicate_publish(distributed_scope):
    space_id, source, keys, redis = distributed_scope
    result = _receive(space_id, source, "worker-message")
    event_ids = _event_ids([str(result.task_id)])
    streams = StreamClient(redis, keys=keys)
    relay_outbox_batch(streams, event_ids=event_ids)
    handled = []
    worker = StreamWorker("ingest", lambda task: handled.append(task["id"]), client=streams, worker_id="worker-a")
    assert worker.run_once(block_ms=10) == 1
    assert handled == [result.task_id]
    with session_scope() as session:
        assert session.get(Task, result.task_id).status == "completed"
        session.execute(update(OutboxEvent).where(OutboxEvent.aggregate_id == result.task_id).values(published_at=None))
    relay_outbox_batch(streams, event_ids=event_ids)
    assert worker.run_once(block_ms=10) == 1
    assert handled == [result.task_id]


def test_failed_task_is_republished_and_then_completes(distributed_scope):
    space_id, source, keys, redis = distributed_scope
    result = _receive(space_id, source, "retry-message")
    streams = StreamClient(redis, keys=keys)
    relay_outbox_batch(streams, event_ids=_event_ids([str(result.task_id)]))
    attempts = []

    def flaky(task):
        attempts.append(task["attempt_count"])
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails")

    worker = StreamWorker("ingest", flaky, client=streams, worker_id="worker-retry")
    assert worker.run_once(block_ms=10) == 1
    time.sleep(2.1)
    assert enqueue_due_retries(limit=10, task_ids=[str(result.task_id)]) == 1
    pending_events = _event_ids([str(result.task_id)])
    relay_outbox_batch(streams, event_ids=pending_events)
    assert worker.run_once(block_ms=10) == 1
    assert attempts == [1, 2]
    with session_scope() as session:
        task = session.get(Task, result.task_id)
        assert task.status == "completed"
        assert task.failure_count == 1
