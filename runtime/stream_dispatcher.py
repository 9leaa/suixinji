"""BoundedTaskExecutor-compatible adapter that persists work to Outbox."""

from __future__ import annotations

from collections.abc import Callable

from core.settings import WORKER_MAX_ATTEMPTS
from repositories.postgres.dispatch import enqueue_task
from runtime.delivery_store import manual_summary_key, query_key
from runtime.task import Task


class StreamTaskDispatcher:
    def submit_query(self, space_id: str, question: str, chat_id: str, message_id: str | None = None) -> Task:
        message_key = message_id or "unknown"
        payload = {
            "question": question,
            "chat_id": chat_id,
            "delivery_key": query_key(space_id, message_key),
            "delivery_type": "query",
        }
        task_id, _ = enqueue_task(
            task_type="query",
            space_id=space_id,
            source_message_id=message_id,
            idempotency_key=f"query:{space_id}:{message_key}",
            payload=payload,
            max_attempts=WORKER_MAX_ATTEMPTS,
        )
        return Task(task_id, "query", space_id, message_id, payload)

    def submit_summary(
        self,
        space_id: str,
        range_key: str,
        chat_id: str,
        message_id: str | None = None,
        on_success: Callable[[], None] | None = None,
        delivery_key: str | None = None,
        delivery_type: str | None = None,
    ) -> Task:
        del on_success
        key = delivery_key or manual_summary_key(space_id, message_id or "scheduled")
        payload = {
            "range_key": range_key,
            "chat_id": chat_id,
            "delivery_key": key,
            "delivery_type": delivery_type or "manual_summary",
        }
        if payload["delivery_type"] == "auto_summary":
            payload["sent_date"] = key.rsplit(":", 1)[-1]
        task_id, _ = enqueue_task(
            task_type="summary",
            space_id=space_id,
            source_message_id=message_id,
            idempotency_key=f"summary:{key}",
            payload=payload,
            max_attempts=WORKER_MAX_ATTEMPTS,
        )
        return Task(task_id, "summary", space_id, message_id, payload)
