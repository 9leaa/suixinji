"""Generic Redis Streams worker with durable PostgreSQL task state."""

from __future__ import annotations

import logging
import socket
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from core.observability import log_event
from core.settings import STREAM_CLAIM_IDLE_MS, WORKER_RETRY_BASE_SECONDS
from repositories.postgres.tasks import claim_task, complete_task, defer_task, fail_task, get_task
from runtime.streams.client import StreamClient, StreamMessage

LOGGER = logging.getLogger(__name__)
TaskHandler = Callable[[dict[str, Any]], None]


def _elapsed_ms(start: datetime | None, end: datetime | None = None) -> int | None:
    if start is None:
        return None
    end = end or datetime.now().astimezone()
    return max(0, int((end - start).total_seconds() * 1000))


class RetryLater(RuntimeError):
    def __init__(self, message: str, delay_seconds: float = 1.0) -> None:
        super().__init__(message)
        self.delay_seconds = delay_seconds


class StreamWorker:
    def __init__(self, task_type: str, handler: TaskHandler, *, client: StreamClient | None = None, worker_id: str | None = None) -> None:
        self.task_type = task_type
        self.handler = handler
        self.client = client or StreamClient()
        self.worker_id = worker_id or f"{socket.gethostname()}-{task_type}-{uuid.uuid4().hex[:8]}"
        self.running = True

    def run_once(self, *, block_ms: int = 1000) -> int:
        messages = self.client.reclaim(self.task_type, self.worker_id, min_idle_ms=STREAM_CLAIM_IDLE_MS)
        if not messages:
            messages = self.client.read(self.task_type, self.worker_id, block_ms=block_ms)
        for message in messages:
            self._handle(message)
        return len(messages)

    def run_forever(self) -> None:
        self.client.ensure_group(self.task_type)
        while self.running:
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("stream worker loop failed: type=%s worker=%s", self.task_type, self.worker_id)
                time.sleep(1)

    def stop(self) -> None:
        self.running = False

    def _handle(self, message: StreamMessage) -> None:
        task_id = str(message.fields.get("task_id") or "")
        if not task_id:
            self.client.dead_letter(message, error="missing task_id")
            self.client.ack(self.task_type, message.message_id)
            return
        task = claim_task(task_id, self.worker_id, stale_after_seconds=max(1, STREAM_CLAIM_IDLE_MS // 1000))
        if task is None:
            existing = get_task(task_id)
            if existing is None or existing.get("status") in {"completed", "dead_letter"}:
                self.client.ack(self.task_type, message.message_id)
            return
        execution_started = time.perf_counter()
        queue_wait_ms = _elapsed_ms(task.get("created_at"), task.get("started_at"))
        common_extra = {
            "task_id": task_id,
            "task_type": self.task_type,
            "worker_id": self.worker_id,
            "attempt_count": int(task.get("attempt_count") or 0),
            "queue_wait_ms": queue_wait_ms,
        }
        log_event(
            "runtime.stream_task_started",
            status="running",
            space_id=str(task.get("space_id") or "") or None,
            message_id=task.get("source_message_id"),
            record_id=task_id,
            extra=common_extra,
        )
        try:
            self.handler(task)
        except RetryLater as exc:
            defer_task(task_id, str(exc), retry_delay_seconds=exc.delay_seconds)
            self.client.ack(self.task_type, message.message_id)
            execution_ms = int((time.perf_counter() - execution_started) * 1000)
            log_event(
                "runtime.stream_task_deferred",
                level="warning",
                status="retry",
                space_id=str(task.get("space_id") or "") or None,
                message_id=task.get("source_message_id"),
                record_id=task_id,
                duration_ms=execution_ms,
                error=type(exc).__name__,
                extra={**common_extra, "execution_ms": execution_ms, "retry_delay_seconds": exc.delay_seconds},
            )
            return
        except Exception as exc:
            attempt = int(task.get("attempt_count") or 1)
            delay = WORKER_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1))
            error = f"{type(exc).__name__}: {exc}"
            status = fail_task(task_id, error, retry_delay_seconds=delay)
            if status == "dead_letter":
                self.client.dead_letter(message, error=error)
            self.client.ack(self.task_type, message.message_id)
            execution_ms = int((time.perf_counter() - execution_started) * 1000)
            log_event(
                "runtime.stream_task_failed",
                level="error",
                status=status,
                space_id=str(task.get("space_id") or "") or None,
                message_id=task.get("source_message_id"),
                record_id=task_id,
                duration_ms=execution_ms,
                error=type(exc).__name__,
                extra={**common_extra, "execution_ms": execution_ms, "retry_delay_seconds": delay},
            )
            LOGGER.exception("stream task failed: task_id=%s task_type=%s status=%s", task_id, self.task_type, status)
            return
        complete_task(task_id)
        self.client.ack(self.task_type, message.message_id)
        finished_at = datetime.now().astimezone()
        execution_ms = int((time.perf_counter() - execution_started) * 1000)
        log_event(
            "runtime.stream_task_completed",
            status="completed",
            space_id=str(task.get("space_id") or "") or None,
            message_id=task.get("source_message_id"),
            record_id=task_id,
            duration_ms=execution_ms,
            extra={
                **common_extra,
                "execution_ms": execution_ms,
                "total_duration_ms": _elapsed_ms(task.get("created_at"), finished_at),
            },
        )
