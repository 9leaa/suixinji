"""Platform-neutral Receiver that persists Inbox + Task + Outbox atomically."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.settings import COORDINATION_BACKEND, WORKER_MAX_ATTEMPTS
from infrastructure.redis_idempotency import IdempotencyStore
from infrastructure.redis_keys import KEYS
from repositories.postgres.dispatch import DispatchResult, receive_command


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
    idem_key = KEYS.idempotency(command.source, command.message_id)
    if COORDINATION_BACKEND == "redis":
        try:
            store = IdempotencyStore()
            if store.get(idem_key) == "completed":
                return DispatchResult("redis-completed", None, False, True)
            store.begin(idem_key)
        except Exception:
            store = None
    try:
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
            task_payload=command.task_payload,
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
