from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, select, update

from infrastructure.database import session_scope
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import RedisKeys
from infrastructure.schema import InboxMessage, OutboxEvent, Space, Task
from repositories.postgres.dispatch import is_next_inbox_message, mark_inbox_processed, receive_command
from repositories.postgres.outbox import relay_outbox_batch
from repositories.postgres.tasks import enqueue_due_retries
from runtime.streams import StreamClient
from runtime.streams.worker import StreamWorker

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


def _receive(space_id: str, source: str, message_id: str):
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
        task_type="ingest",
        task_payload={"chat_id": "chat-test"},
    )


def _event_ids(task_ids: list[str]) -> list[str]:
    with session_scope() as session:
        return list(session.execute(select(OutboxEvent.id).where(OutboxEvent.aggregate_id.in_(task_ids))).scalars())


def test_transactional_receiver_has_one_inbox_task_and_outbox(distributed_scope):
    space_id, source, _keys, _client = distributed_scope
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _index: _receive(space_id, source, "same-message"), range(10)))
    assert sum(result.created for result in results) == 1
    with session_scope() as session:
        assert len(list(session.execute(select(InboxMessage).where(InboxMessage.space_id == space_id)).scalars())) == 1
        tasks = list(session.execute(select(Task).where(Task.space_id == space_id)).scalars())
        assert len(tasks) == 1
        assert session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == tasks[0].id)).scalar_one() is not None


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


def test_pending_message_can_be_reclaimed_and_sequence_is_enforced(distributed_scope):
    space_id, source, keys, redis = distributed_scope
    first = _receive(space_id, source, "sequence-1")
    second = _receive(space_id, source, "sequence-2")
    assert is_next_inbox_message(first.inbox_id)
    assert not is_next_inbox_message(second.inbox_id)
    mark_inbox_processed(first.inbox_id)
    assert is_next_inbox_message(second.inbox_id)
    streams = StreamClient(redis, keys=keys)
    relay_outbox_batch(streams, limit=10, event_ids=_event_ids([str(first.task_id), str(second.task_id)]))
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
        assert session.get(Task, result.task_id).status == "completed"
