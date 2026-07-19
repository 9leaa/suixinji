from __future__ import annotations

import os
import uuid
from contextlib import nullcontext

import pytest
from sqlalchemy import delete, select

from apps import handlers
from core.settings import DATABASE_GLOBAL_BUDGET, database_pool_budget
from infrastructure.database import session_scope
from infrastructure.schema import InboxMessage, MemoryExtractionState, MemoryTrace, OutboxEvent, Space, Task, Tenant
from repositories.postgres.common import ensure_tenant_space
from repositories.postgres import memory as postgres_memory
from repositories.postgres.dispatch import receive_command
from repositories.postgres.tasks import claim_task, complete_task
from runtime.streams.client import StreamMessage
from runtime.streams.worker import AdaptiveStreamWorker


def test_stage5_adaptive_process_matrix_stays_within_connection_budget() -> None:
    roles = {
        "receiver": 2,
        "outbox-relay": 4,
        "worker-adaptive": 12,
        "scheduler": 2,
    }
    theoretical_peak = sum(count * sum(database_pool_budget(role)) for role, count in roles.items())
    assert theoretical_peak == 32
    assert theoretical_peak <= DATABASE_GLOBAL_BUDGET


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="PostgreSQL integration URL is not configured")
def test_task_cannot_complete_another_tenants_inbox() -> None:
    suffix = uuid.uuid4().hex
    tenant_a = f"stage5-tenant-a-{suffix}"
    tenant_b = f"stage5-tenant-b-{suffix}"

    def receive(tenant_id: str):
        return receive_command(
            source="stage5",
            source_message_id=f"message-{tenant_id}",
            source_event_id=None,
            tenant_id=tenant_id,
            space_id="shared-source-space",
            chat_id=None,
            chat_type=None,
            sender={},
            text_value="test",
            received_at="2026-07-18T12:00:00+08:00",
            task_type="ingest",
            task_payload={},
        )

    try:
        result_a = receive(tenant_a)
        result_b = receive(tenant_b)
        claimed = claim_task(str(result_a.task_id), "stage5-tenant-guard")
        assert claimed is not None
        lease = {
            "lease_token": str(claimed["lease_token"]),
            "claim_version": int(claimed["claim_version"]),
        }

        assert complete_task(
            str(result_a.task_id),
            ingest_complete_inbox_id=result_b.inbox_id,
            **lease,
        ) is False
        with session_scope() as session:
            assert session.get(Task, result_a.task_id).status == "running"
            assert session.get(InboxMessage, result_b.inbox_id).status == "pending"
        assert complete_task(
            str(result_a.task_id),
            ingest_complete_inbox_id=result_a.inbox_id,
            **lease,
        ) is True
    finally:
        with session_scope() as session:
            task_ids = list(
                session.execute(select(Task.id).where(Task.tenant_id.in_([tenant_a, tenant_b]))).scalars()
            )
            if task_ids:
                session.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id.in_(task_ids)))
            session.execute(delete(Tenant).where(Tenant.id.in_([tenant_a, tenant_b])))


def test_rules_empty_memory_uses_inline_causal_fast_path(monkeypatch) -> None:
    enqueued: list[str] = []
    processed: list[str] = []
    note = {
        "id": "note-stage5",
        "message_id": "message-stage5",
        "space_id": "space-stage5",
        "tenant_id": "tenant-stage5",
        "text": "ordinary note without a memory candidate",
        "title": "ordinary note",
        "tags": [],
        "type": "other",
        "summary": "ordinary note",
    }
    monkeypatch.setattr(handlers, "load_inbox_record", lambda _inbox_id: {"id": "inbox-stage5"})
    monkeypatch.setattr(handlers, "coordinated_lock", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(handlers, "process_record", lambda *_args, **_kwargs: note)
    monkeypatch.setattr(handlers, "may_contain_memory", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        handlers,
        "process_note_memory",
        lambda item, classification=None: processed.append(str(item["id"])),
    )

    def enqueue(*, task_type: str, **_kwargs):
        enqueued.append(task_type)
        return f"task-{task_type}", True

    monkeypatch.setattr(handlers, "enqueue_task", enqueue)
    outcome = handlers.handle_ingest(
        {
            "id": "task-ingest",
            "tenant_id": "tenant-stage5",
            "space_id": "space-stage5",
            "source_message_id": "message-stage5",
            "payload_json": {"inbox_id": "inbox-stage5", "sequence_no": 1},
        }
    )

    assert processed == ["note-stage5"]
    assert enqueued == ["enrichment"]
    assert outcome.ingest_complete_inbox_id == "inbox-stage5"
    assert outcome.note_ready_inbox_id is None


def test_memory_handler_preserves_note_classification_for_model_extraction(monkeypatch) -> None:
    note = {
        "id": "note-model-context",
        "space_id": "space-model-context",
        "tenant_id": "tenant-model-context",
        "text": "model extraction input",
        "title": "Project decision",
        "tags": ["architecture", "decision"],
        "type": "work",
        "summary": "Use PostgreSQL for durable state",
    }
    captured: list[dict] = []
    monkeypatch.setattr(handlers, "find_note_content", lambda *_args: note)
    monkeypatch.setattr(
        handlers,
        "process_note_memory",
        lambda _note, classification=None: captured.append(classification or {}),
    )

    outcome = handlers.handle_memory(
        {
            "id": "task-model-context",
            "tenant_id": "tenant-model-context",
            "space_id": "space-model-context",
            "payload_json": {
                "operation": "extract",
                "note_id": "note-model-context",
                "barrier_inbox_id": "inbox-model-context",
            },
        }
    )

    assert captured == [
        {
            "title": "Project decision",
            "tags": ["architecture", "decision"],
            "type": "work",
            "summary": "Use PostgreSQL for durable state",
        }
    ]
    assert outcome is not None
    assert outcome.memory_ready_inbox_id == "inbox-model-context"


def test_adaptive_worker_polls_and_handles_multiple_stream_groups() -> None:
    messages = [
        StreamMessage("ingest-stream", "1-0", {"task_id": "ingest-1", "task_type": "ingest"}),
        StreamMessage("memory-stream", "2-0", {"task_id": "memory-1", "task_type": "memory"}),
    ]

    class FakeClient:
        def __init__(self) -> None:
            self.orders: list[list[str]] = []

        def read_many(self, task_types, _consumer, *, count=1):
            self.orders.append(list(task_types))
            return messages if len(self.orders) == 1 else []

    client = FakeClient()
    worker = AdaptiveStreamWorker(
        {"ingest": lambda _task: None, "memory": lambda _task: None},
        client=client,
        worker_id="adaptive-test",
    )
    handled: list[tuple[str, str]] = []
    for task_type, stream_worker in worker.workers.items():
        stream_worker._handle = lambda message, resolved=task_type: handled.append((resolved, message.message_id))
        stream_worker._next_reclaim_at = float("inf")

    assert worker.run_once() == 2
    assert handled == [("ingest", "1-0"), ("memory", "2-0")]
    assert worker.run_once() == 0
    assert client.orders == [["ingest", "memory"], ["memory", "ingest"]]


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="PostgreSQL integration URL is not configured")
def test_memory_extraction_and_trace_keep_the_internal_tenant_space() -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"stage5-tenant-{suffix}"
    source_space_id = f"stage5-source-space-{suffix}"
    note_id = f"stage5-note-{suffix}"
    trace_id = f"stage5-trace-{suffix}"
    try:
        with session_scope() as session:
            internal_space_id = ensure_tenant_space(
                session,
                source_space_id,
                tenant_id=tenant_id,
                source="stage5",
            )
        state = postgres_memory.mark_extraction_empty_attempt(note_id, internal_space_id)
        postgres_memory.save_memory_trace(
            {
                "trace_id": trace_id,
                "trace_type": "memory_write",
                "space_id": internal_space_id,
                "note_id": note_id,
                "status": "success",
                "started_at": "2026-07-18T12:00:00+08:00",
                "finished_at": "2026-07-18T12:00:01+08:00",
                "steps": [],
            }
        )
        assert state.space_id == internal_space_id
        with session_scope() as session:
            extraction_space = session.get(MemoryExtractionState, note_id).space_id
            trace_space = session.get(MemoryTrace, trace_id).space_id
            assert extraction_space == internal_space_id
            assert trace_space == internal_space_id
            assert session.get(Space, internal_space_id).tenant_id == tenant_id
            assert session.execute(
                select(Space.id).where(
                    Space.tenant_id == "default",
                    Space.source_space_id == internal_space_id,
                )
            ).scalar_one_or_none() is None
    finally:
        with session_scope() as session:
            session.execute(delete(Tenant).where(Tenant.id == tenant_id))
