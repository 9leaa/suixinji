"""Platform-neutral Receiver that persists Inbox + Task + Outbox atomically."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.settings import COORDINATION_BACKEND, WORKER_MAX_ATTEMPTS
from infrastructure.redis_idempotency import IdempotencyStore
from infrastructure.redis_keys import KEYS
from repositories.postgres.dispatch import DispatchResult, receive_command
from runtime.consistency import task_consistency


@dataclass(frozen=True)
class InboxCommand:
    source: str
    message_id: str
    space_id: str
    text: str
    task_type: str
    task_payload: dict[str, Any]
    event_id: str | None = None
    tenant_id: str = "default"
    chat_id: str | None = None
    chat_type: str | None = None
    sender: dict[str, Any] = field(default_factory=dict)
    received_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())


def receive(command: InboxCommand) -> DispatchResult:
    store = None
    idem_key = KEYS.idempotency(command.tenant_id, command.source, command.message_id)
    if COORDINATION_BACKEND == "redis":
        try:
            store = IdempotencyStore()
            state = store.get(idem_key)
            if state == "completed":
                return DispatchResult("redis-completed", None, False, True)
            if state == "processing":
                return DispatchResult("redis-processing", None, False, False, True)
            if not store.begin(idem_key):
                return DispatchResult("redis-processing", None, False, False, True)
        except Exception:
            store = None
    try:
        task_payload = dict(command.task_payload)
        task_payload["consistency"] = task_consistency(command.task_type, task_payload)
        result = receive_command(
            source=command.source,
            source_message_id=command.message_id,
            source_event_id=command.event_id,
            tenant_id=command.tenant_id,
            space_id=command.space_id,
            chat_id=command.chat_id,
            chat_type=command.chat_type,
            sender=command.sender,
            text_value=command.text,
            received_at=command.received_at,
            task_type=command.task_type,
            task_payload=task_payload,
            max_attempts=WORKER_MAX_ATTEMPTS,
        )
    except Exception:
        if store is not None:
            try:
                store.fail(idem_key)
            except Exception:
                pass
        raise
    if store is not None:
        try:
            store.complete(idem_key)
        except Exception:
            pass
    return result
