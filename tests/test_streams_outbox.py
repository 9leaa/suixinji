from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

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


def _lease(claimed: dict) -> dict[str, object]:
    return {
        "lease_token": str(claimed["lease_token"]),
        "claim_version": int(claimed["claim_version"]),
    }


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

    claimed = claim_task(str(first.task_id), "ordered-worker")
    assert claimed is not None
    assert complete_task(str(first.task_id), note_ready_inbox_id=first.inbox_id, **_lease(claimed)) is True

    with session_scope() as session:
        assert session.get(Task, first.task_id).status == "completed"
        assert session.get(Task, second.task_id).status == "queued"
        assert session.get(Task, third.task_id).status == "blocked"
    assert _outbox_count(str(second.task_id)) == 1
    assert _outbox_count(str(third.task_id)) == 0
    assert get_space_progress(space_id) == {
        "processed_sequence_no": 0,
        "note_watermark": 1,
        "memory_watermark": 0,
        "memory_gap_sequence_no": None,
    }


def test_concurrent_completion_cannot_publish_the_next_root_twice(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    first = _receive(space_id, source, "concurrent-1")
    second = _receive(space_id, source, "concurrent-2")
    third = _receive(space_id, source, "concurrent-3")
    claimed = claim_task(str(first.task_id), "concurrent-worker")
    assert claimed is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: complete_task(
                    str(first.task_id),
                    note_ready_inbox_id=first.inbox_id,
                    **_lease(claimed),
                ),
                range(2),
            )
        )
    assert sorted(results) == [False, True]

    with session_scope() as session:
        assert session.get(Task, second.task_id).status == "queued"
        assert session.get(Task, third.task_id).status == "blocked"
    assert _outbox_count(str(second.task_id)) == 1
    assert _outbox_count(str(third.task_id)) == 0


def test_expired_worker_cannot_complete_after_task_is_reclaimed(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    result = _receive(space_id, source, "fencing-message")
    claimed_a = claim_task(str(result.task_id), "worker-a", stale_after_seconds=30)
    assert claimed_a is not None
    with session_scope() as session:
        session.execute(
            update(Task)
            .where(Task.id == result.task_id)
            .values(lease_expires_at=datetime.now().astimezone() - timedelta(seconds=1))
        )
    claimed_b = claim_task(str(result.task_id), "worker-b", stale_after_seconds=30)
    assert claimed_b is not None
    assert claimed_b["claim_version"] == claimed_a["claim_version"] + 1

    assert complete_task(
        str(result.task_id),
        note_ready_inbox_id=result.inbox_id,
        **_lease(claimed_a),
    ) is False
    assert get_task(str(result.task_id))["claimed_by"] == "worker-b"
    assert complete_task(
        str(result.task_id),
        note_ready_inbox_id=result.inbox_id,
        **_lease(claimed_b),
    ) is True


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
            "question": "我喜欢什么",
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
    monkeypatch.setattr(handlers, "extract_candidates", lambda *_args, **_kwargs: ["candidate"])

    outcome = handlers.handle_ingest(root)
    assert isinstance(outcome, TaskOutcome)
    assert outcome.note_ready_inbox_id == first.inbox_id

    with session_scope() as session:
        memory_tasks = list(
            session.execute(select(Task).where(Task.space_id == space_id, Task.task_type == "memory")).scalars()
        )
        assert len(memory_tasks) == 1
        critical = memory_tasks[0]
        critical_task_id = critical.id
        enrich = session.execute(
            select(Task).where(Task.space_id == space_id, Task.task_type == "enrichment")
        ).scalar_one()
        assert [row.id for row in memory_tasks] == [critical_task_id]
        assert critical.status == "queued"
        assert critical.payload_json["barrier_inbox_id"] == first.inbox_id
        assert enrich.status == "queued"
        assert session.get(Task, second.task_id).status == "blocked"
    assert get_space_progress(space_id)["memory_watermark"] == 0

    assert complete_task(
        str(first.task_id),
        note_ready_inbox_id=first.inbox_id,
        **_lease(root),
    ) is True
    with session_scope() as session:
        assert session.get(Task, critical_task_id).status == "queued"
        assert session.get(Task, second.task_id).status == "blocked"
    assert _outbox_count(critical_task_id) == 1
    assert get_space_progress(space_id)["memory_watermark"] == 0
    assert get_space_progress(space_id)["note_watermark"] == 2

    critical = claim_task(critical_task_id, "memory-worker")
    assert critical is not None
    complete_task(
        critical_task_id,
        memory_ready_inbox_id=str(critical["payload_json"]["barrier_inbox_id"]),
        **_lease(critical),
    )

    with session_scope() as session:
        assert session.get(InboxMessage, first.inbox_id).status == "processed"
        assert session.get(Task, second.task_id).status == "queued"
        enrich = session.execute(
            select(Task).where(Task.space_id == space_id, Task.task_type == "enrichment")
        ).scalar_one()
        assert enrich.status == "queued"
    assert get_space_progress(space_id) == {
        "processed_sequence_no": 1,
        "note_watermark": 2,
        "memory_watermark": 2,
        "memory_gap_sequence_no": None,
    }


def test_terminal_critical_memory_failure_records_gap_and_releases_next(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    first = _receive(space_id, source, "gap-1")
    second = _receive(
        space_id,
        source,
        "gap-2",
        task_type="query",
        payload={"question": "我喜欢什么"},
    )
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
        initial_status="queued",
        publish=True,
    )
    assert created
    root = claim_task(str(first.task_id), "gap-ingest")
    assert root is not None
    complete_task(str(first.task_id), note_ready_inbox_id=first.inbox_id, **_lease(root))
    critical = claim_task(critical_task_id, "gap-memory")
    assert critical is not None

    assert fail_task(
        critical_task_id,
        "RuntimeError: extraction failed",
        retry_delay_seconds=0.1,
        **_lease(critical),
    ) == "dead_letter"

    with session_scope() as session:
        assert session.get(InboxMessage, first.inbox_id).status == "failed"
        assert session.get(Task, second.task_id).status == "queued"
        space = session.get(Space, space_id)
        assert space.metadata_json["last_memory_gap"]["inbox_id"] == first.inbox_id
    assert get_space_progress(space_id) == {
        "processed_sequence_no": 1,
        "note_watermark": 2,
        "memory_watermark": 2,
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
    root = claim_task(str(first.task_id), "root-gap-worker")
    assert root is not None

    assert fail_task(
        str(first.task_id),
        "RuntimeError: ingest failed",
        retry_delay_seconds=0.1,
        **_lease(root),
    ) == "dead_letter"

    with session_scope() as session:
        assert session.get(Task, critical_task_id).status == "cancelled"
        assert session.get(Task, second.task_id).status == "queued"
        assert session.get(InboxMessage, first.inbox_id).status == "failed"


def test_defer_does_not_consume_failure_budget(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    result = _receive(space_id, source, "defer-message", max_attempts=1)
    deferred_claim = claim_task(str(result.task_id), "defer-worker")
    assert deferred_claim is not None
    defer_task(str(result.task_id), "dependency busy", retry_delay_seconds=0.1, **_lease(deferred_claim))

    task = get_task(str(result.task_id))
    assert task is not None
    assert task["status"] == "retry"
    assert task["defer_count"] == 1
    assert task["failure_count"] == 0

    time.sleep(0.11)
    assert enqueue_due_retries(limit=1, task_ids=[str(result.task_id)]) == 1
    failed_claim = claim_task(str(result.task_id), "failure-worker")
    assert failed_claim is not None
    assert fail_task(
        str(result.task_id),
        "RuntimeError: real failure",
        retry_delay_seconds=0.1,
        **_lease(failed_claim),
    ) == "dead_letter"
    task = get_task(str(result.task_id))
    assert task["defer_count"] == 1
    assert task["failure_count"] == 1


def test_stream_group_cache_recovers_after_stream_recreation(distributed_scope):
    _space_id, _source, keys, redis = distributed_scope
    streams = StreamClient(redis, keys=keys)
    stream, _group = streams.ensure_group("ingest")
    redis.delete(stream)
    streams.publish_task("event-recreated", {"task_id": "task-recreated", "task_type": "ingest"})

    messages = streams.read("ingest", "recovery-worker", count=1, block_ms=1)

    assert [message.fields["task_id"] for message in messages] == ["task-recreated"]


def test_multi_stream_read_recovers_only_missing_groups(distributed_scope):
    _space_id, _source, keys, redis = distributed_scope
    streams = StreamClient(redis, keys=keys)
    ingest_stream, _group = streams.ensure_group("ingest")
    streams.ensure_group("query")
    redis.delete(ingest_stream)
    streams.publish_task("event-ingest", {"task_id": "task-ingest", "task_type": "ingest"})
    streams.publish_task("event-query", {"task_id": "task-query", "task_type": "query"})

    messages = streams.read_many(["ingest", "query"], "adaptive-recovery", count=1)

    assert {message.fields["task_id"] for message in messages} == {"task-ingest", "task-query"}

def test_outbox_relay_duplicate_publish_keeps_one_task(distributed_scope):
    space_id, source, keys, redis = distributed_scope
    result = _receive(space_id, source, "relay-message")
    streams = StreamClient(redis, keys=keys)
    event_ids = _event_ids([str(result.task_id)])
    assert relay_outbox_batch(streams, limit=10, event_ids=event_ids) == {
        "published": 1,
        "failed": 0,
        "dead": 0,
        "stale": 0,
    }
    messages = streams.read("ingest", "consumer-a", count=10, block_ms=10)
    assert len(messages) == 1
    assert messages[0].fields["task_id"] == result.task_id
    with session_scope() as session:
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.aggregate_id == result.task_id)
            .values(published_at=None, status="pending")
        )
    assert relay_outbox_batch(streams, limit=10, event_ids=event_ids)["published"] == 1
    duplicate = streams.read("ingest", "consumer-a", count=10, block_ms=10)
    assert duplicate[0].fields["task_id"] == result.task_id
    with session_scope() as session:
        assert len(list(session.execute(select(Task).where(Task.space_id == space_id)).scalars())) == 1


def test_outbox_publish_happens_without_holding_event_row_lock(distributed_scope):
    space_id, source, keys, redis = distributed_scope
    result = _receive(space_id, source, "relay-unlocked")
    streams = StreamClient(redis, keys=keys)

    class InspectingPublisher:
        def publish_task(self, event_id, payload):
            with session_scope() as session:
                assert session.execute(
                    select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update(nowait=True)
                ).scalar_one() is not None
            return streams.publish_task(event_id, payload)

    report = relay_outbox_batch(InspectingPublisher(), event_ids=_event_ids([str(result.task_id)]))
    assert report["published"] == 1
    assert report["failed"] == 0


def test_poison_outbox_event_moves_to_dead_without_blocking_batch(distributed_scope):
    space_id, source, _keys, _redis = distributed_scope
    result = _receive(space_id, source, "poison-1")
    with session_scope() as session:
        event = session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == result.task_id)).scalar_one()
        event.max_attempts = 1

    class FailingPublisher:
        def publish_task(self, event_id, payload):
            raise TimeoutError("redis unavailable")

    report = relay_outbox_batch(FailingPublisher(), event_ids=_event_ids([str(result.task_id)]))
    assert report == {"published": 0, "failed": 1, "dead": 1, "stale": 0}
    with session_scope() as session:
        event = session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == result.task_id)).scalar_one()
        assert event.status == "dead"
        assert event.failed_at is not None


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
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.aggregate_id == result.task_id)
            .values(published_at=None, status="pending")
        )
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
