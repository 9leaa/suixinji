from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from apps import api, handlers, receiver
from apps.api import ReceiveRequest
from infrastructure.database import session_scope
from infrastructure.schema import Memory, Tenant
from memory.models import MemoryCandidate
from repositories.postgres import memory as postgres_memory
from repositories.postgres.common import ensure_tenant_space
from repositories.postgres.dispatch import DispatchResult


def _api_request(task_type: str = "ingest") -> ReceiveRequest:
    return ReceiveRequest(
        message_id="stage4-message",
        space_id="stage4-space",
        text="hello",
        task_type=task_type,
        user_id="stage4-user",
    )


def test_api_keeps_accepting_when_redis_rate_limit_is_unavailable(monkeypatch):
    class BrokenLimiter:
        def allow(self, *_args, **_kwargs):
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(api, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(api, "RedisRateLimiter", BrokenLimiter)
    monkeypatch.setattr(api, "database_overload_snapshot", lambda: SimpleNamespace(state="normal", to_dict=lambda: {}))
    api._check_rate_limit(_api_request())


def test_api_delays_summary_when_redis_rate_limit_is_unavailable(monkeypatch):
    class BrokenLimiter:
        def allow(self, *_args, **_kwargs):
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(api, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(api, "RedisRateLimiter", BrokenLimiter)
    monkeypatch.setattr(api, "database_overload_snapshot", lambda: SimpleNamespace(state="normal", to_dict=lambda: {}))
    with pytest.raises(HTTPException) as exc_info:
        api._check_rate_limit(_api_request("summary"))
    assert exc_info.value.status_code == 503


def test_api_returns_429_for_measured_rate_limit(monkeypatch):
    class RejectingLimiter:
        def allow(self, *_args, **_kwargs):
            return SimpleNamespace(allowed=False, retry_after_ms=1500)

    monkeypatch.setattr(api, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(api, "RedisRateLimiter", RejectingLimiter)
    monkeypatch.setattr(api, "database_overload_snapshot", lambda: SimpleNamespace(state="normal", to_dict=lambda: {}))
    with pytest.raises(HTTPException) as exc_info:
        api._check_rate_limit(_api_request())
    assert exc_info.value.status_code == 429


def test_api_ignores_local_database_pressure_when_redis_rate_limit_is_available(monkeypatch):
    class AllowingLimiter:
        def allow(self, *_args, **_kwargs):
            return SimpleNamespace(allowed=True, retry_after_ms=0)

    monkeypatch.setattr(api, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(api, "RedisRateLimiter", AllowingLimiter)
    monkeypatch.setattr(
        api,
        "database_overload_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("DB backpressure is only a Redis-outage fallback")),
    )
    api._check_rate_limit(_api_request("query"))


def test_api_rejects_query_when_redis_and_database_are_unavailable(monkeypatch):
    class BrokenLimiter:
        def allow(self, *_args, **_kwargs):
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(api, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(api, "RedisRateLimiter", BrokenLimiter)
    monkeypatch.setattr(api, "database_overload_snapshot", lambda: SimpleNamespace(state="overload", to_dict=lambda: {}))
    with pytest.raises(HTTPException) as exc_info:
        api._check_rate_limit(_api_request("query"))
    assert exc_info.value.status_code == 503


def test_api_returns_retryable_503_when_receiver_pool_times_out(monkeypatch):
    monkeypatch.setattr(api, "_check_rate_limit", lambda _request: None)
    monkeypatch.setattr(api, "receive", lambda _command: (_ for _ in ()).throw(SQLAlchemyTimeoutError()))
    monkeypatch.setattr(api, "database_overload_snapshot", lambda: SimpleNamespace(state="overload", to_dict=lambda: {}))
    with pytest.raises(HTTPException) as exc_info:
        api.commands(_api_request())
    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "2"}


def test_receiver_falls_back_to_postgres_when_redis_idempotency_is_down(monkeypatch):
    class BrokenIdempotency:
        def __init__(self):
            raise ConnectionError("redis unavailable")

    expected = DispatchResult("inbox-1", "task-1", True, False)
    monkeypatch.setattr(receiver, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(receiver, "IdempotencyStore", BrokenIdempotency)
    monkeypatch.setattr(receiver, "receive_command", lambda **_kwargs: expected)
    result = receiver.receive(
        receiver.InboxCommand(
            source="stage4",
            message_id="message-1",
            space_id="space-1",
            text="hello",
            task_type="ingest",
            task_payload={},
        )
    )
    assert result == expected


def test_receiver_returns_in_progress_without_postgres_for_processing_idempotency(monkeypatch):
    class ProcessingIdempotency:
        def get(self, _key):
            return "processing"

    monkeypatch.setattr(receiver, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(receiver, "IdempotencyStore", ProcessingIdempotency)
    monkeypatch.setattr(
        receiver,
        "receive_command",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not access PostgreSQL")),
    )
    result = receiver.receive(
        receiver.InboxCommand(
            source="stage4",
            message_id="message-processing",
            space_id="space-1",
            text="hello",
            task_type="ingest",
            task_payload={},
        )
    )
    assert result.in_progress is True
    assert result.duplicate is False


def test_fake_delivery_never_calls_external_sender(monkeypatch):
    sent = []
    reservation_kwargs = {}

    def reserve(*_args, **kwargs):
        reservation_kwargs.update(kwargs)
        return SimpleNamespace(status="reserved")

    monkeypatch.setattr(handlers, "FAKE_EXTERNALS", True)
    monkeypatch.setattr(handlers, "reserve_delivery", reserve)
    monkeypatch.setattr(handlers, "mark_sent", lambda key: sent.append(key))
    monkeypatch.setattr(handlers, "send_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("external send")))
    handlers.handle_delivery(
        {
            "id": "task-1",
            "tenant_id": "tenant-1",
            "space_id": "space-1",
            "source_message_id": "message-1",
            "payload_json": {
                "delivery_key": "delivery-1",
                "delivery_type": "load",
                "chat_id": "fake-chat",
                "text": "fake text",
            },
        }
    )
    assert sent == ["delivery-1"]
    assert reservation_kwargs["tenant_id"] == "tenant-1"


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="PostgreSQL integration URL is not configured")
def test_memory_insert_inherits_space_tenant():
    suffix = uuid.uuid4().hex
    tenant_id = f"stage4-tenant-{suffix}"
    space_id = f"stage4-space-{suffix}"
    try:
        with session_scope() as session:
            ensure_tenant_space(session, space_id, tenant_id=tenant_id, source="stage4")
        created = postgres_memory.insert_memory(
            space_id,
            MemoryCandidate("preference", "User likes deterministic tests", 0.8, 0.9),
            source_note_id=f"note-{suffix}",
        )
        with session_scope() as session:
            assert session.get(Memory, created.id).tenant_id == tenant_id
    finally:
        with session_scope() as session:
            session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="PostgreSQL integration URL is not configured")
def test_concurrent_space_creation_handles_all_unique_constraints():
    suffix = uuid.uuid4().hex
    tenant_id = f"stage4-tenant-{suffix}"
    space_id = f"stage4-space-{suffix}"

    def create_space(_index: int) -> None:
        with session_scope() as session:
            ensure_tenant_space(session, space_id, tenant_id=tenant_id, source="stage4")

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(create_space, range(24)))
    finally:
        with session_scope() as session:
            session.execute(delete(Tenant).where(Tenant.id == tenant_id))
