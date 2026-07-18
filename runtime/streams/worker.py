"""Generic Redis Streams worker with durable PostgreSQL task state."""

from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.observability import log_event
from core.settings import (
    STREAM_CLAIM_IDLE_MS,
    STREAM_RECLAIM_INTERVAL_SECONDS,
    TASK_LEASE_SECONDS,
    WORKER_RETRY_BASE_SECONDS,
)
from repositories.postgres.tasks import claim_task, complete_task, defer_task, fail_task, get_task, renew_task_lease
from runtime.streams.client import StreamClient, StreamMessage

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskOutcome:
    release_inbox_id: str | None = None
    activate_task_id: str | None = None
    note_ready_inbox_id: str | None = None
    memory_ready_inbox_id: str | None = None
    ingest_complete_inbox_id: str | None = None

    def __post_init__(self) -> None:
        outcomes = (
            self.release_inbox_id,
            self.activate_task_id,
            self.note_ready_inbox_id,
            self.memory_ready_inbox_id,
            self.ingest_complete_inbox_id,
        )
        if sum(value is not None for value in outcomes) > 1:
            raise ValueError("a task may produce only one Inbox/dependency outcome")


TaskHandler = Callable[[dict[str, Any]], TaskOutcome | None]


def _elapsed_ms(start: datetime | None, end: datetime | None = None) -> int | None:
    if start is None:
        return None
    end = end or datetime.now().astimezone()
    return max(0, int((end - start).total_seconds() * 1000))


def _default_outcome(task: dict[str, Any]) -> TaskOutcome:
    payload = dict(task.get("payload_json") or {})
    inbox_id = payload.get("inbox_id")
    return TaskOutcome(release_inbox_id=str(inbox_id)) if inbox_id else TaskOutcome()


class RetryLater(RuntimeError):
    def __init__(self, message: str, delay_seconds: float = 1.0) -> None:
        super().__init__(message)
        self.delay_seconds = delay_seconds


class StreamWorker:
    def __init__(
        self,
        task_type: str,
        handler: TaskHandler,
        *,
        client: StreamClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.task_type = task_type
        self.handler = handler
        self.client = client or StreamClient()
        self.worker_id = worker_id or f"{socket.gethostname()}-{task_type}-{uuid.uuid4().hex[:8]}"
        self.running = True
        stagger = (hash(self.worker_id) & 0xFFFF) / 0xFFFF * max(0.1, STREAM_RECLAIM_INTERVAL_SECONDS)
        self._next_reclaim_at = time.monotonic() + stagger

    def run_once(self, *, block_ms: int = 1000) -> int:
        messages = self.client.read(self.task_type, self.worker_id, block_ms=block_ms)
        now = time.monotonic()
        if not messages and now >= self._next_reclaim_at:
            reclaim_started = time.perf_counter()
            messages = self.client.reclaim(self.task_type, self.worker_id, min_idle_ms=STREAM_CLAIM_IDLE_MS)
            self._next_reclaim_at = now + max(0.1, STREAM_RECLAIM_INTERVAL_SECONDS)
            log_event(
                "runtime.stream_reclaim",
                status="completed",
                duration_ms=int((time.perf_counter() - reclaim_started) * 1000),
                extra={
                    "task_type": self.task_type,
                    "worker_id": self.worker_id,
                    "reclaim_count": len(messages),
                    "next_start_id": self.client.reclaim_cursor(self.task_type, self.worker_id),
                    "min_idle_ms": STREAM_CLAIM_IDLE_MS,
                },
            )
        for message in messages:
            self._handle(message)
        return len(messages)

    def run_forever(self) -> None:
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
        task = claim_task(task_id, self.worker_id, stale_after_seconds=TASK_LEASE_SECONDS)
        if task is None:
            existing = get_task(task_id)
            if existing is None or existing.get("status") in {
                "blocked",
                "cancelled",
                "completed",
                "dead_letter",
                "retry",
            }:
                self.client.ack(self.task_type, message.message_id)
            return
        lease_token = str(task["lease_token"])
        claim_version = int(task["claim_version"])
        stop_heartbeat = threading.Event()
        ownership_lost = threading.Event()

        def renew_lease() -> None:
            interval = max(0.5, TASK_LEASE_SECONDS / 3)
            while not stop_heartbeat.wait(interval):
                try:
                    if not renew_task_lease(
                        task_id,
                        lease_token=lease_token,
                        claim_version=claim_version,
                        lease_seconds=TASK_LEASE_SECONDS,
                    ):
                        ownership_lost.set()
                        return
                except Exception:
                    LOGGER.warning("task lease renewal failed: task_id=%s", task_id, exc_info=True)

        heartbeat = threading.Thread(target=renew_lease, name=f"task-lease-{task_id[-8:]}", daemon=True)
        heartbeat.start()

        def finish_heartbeat() -> None:
            stop_heartbeat.set()
            heartbeat.join(timeout=1)

        execution_started = time.perf_counter()
        queue_wait_ms = _elapsed_ms(task.get("created_at"), task.get("started_at"))
        common_extra = {
            "task_id": task_id,
            "task_type": self.task_type,
            "worker_id": self.worker_id,
            "attempt_count": int(task.get("attempt_count") or 0),
            "failure_count": int(task.get("failure_count") or 0),
            "defer_count": int(task.get("defer_count") or 0),
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
            outcome = self.handler(task)
            if outcome is None:
                outcome = _default_outcome(task)
            elif not isinstance(outcome, TaskOutcome):
                raise TypeError(f"task handler returned unsupported outcome: {type(outcome).__name__}")
        except RetryLater as exc:
            finish_heartbeat()
            deferred = defer_task(
                task_id,
                str(exc),
                retry_delay_seconds=exc.delay_seconds,
                lease_token=lease_token,
                claim_version=claim_version,
            )
            if deferred:
                self.client.ack(self.task_type, message.message_id)
            execution_ms = int((time.perf_counter() - execution_started) * 1000)
            log_event(
                "runtime.stream_task_deferred",
                level="warning",
                status="retry" if deferred else "stale",
                space_id=str(task.get("space_id") or "") or None,
                message_id=task.get("source_message_id"),
                record_id=task_id,
                duration_ms=execution_ms,
                error=type(exc).__name__,
                extra={
                    **common_extra,
                    "defer_count": int(task.get("defer_count") or 0) + 1,
                    "execution_ms": execution_ms,
                    "retry_delay_seconds": exc.delay_seconds,
                },
            )
            return
        except Exception as exc:
            finish_heartbeat()
            failure_no = int(task.get("failure_count") or 0) + 1
            delay = WORKER_RETRY_BASE_SECONDS * (2 ** max(0, failure_no - 1))
            error = f"{type(exc).__name__}: {exc}"
            status = fail_task(
                task_id,
                error,
                retry_delay_seconds=delay,
                lease_token=lease_token,
                claim_version=claim_version,
            )
            if status == "dead_letter":
                self.client.dead_letter(message, error=error)
            if status != "stale":
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
                extra={
                    **common_extra,
                    "failure_count": failure_no,
                    "execution_ms": execution_ms,
                    "retry_delay_seconds": delay,
                },
            )
            LOGGER.exception("stream task failed: task_id=%s task_type=%s status=%s", task_id, self.task_type, status)
            return
        finish_heartbeat()
        completed = complete_task(
            task_id,
            lease_token=lease_token,
            claim_version=claim_version,
            release_inbox_id=outcome.release_inbox_id,
            activate_task_id=outcome.activate_task_id,
            note_ready_inbox_id=outcome.note_ready_inbox_id,
            memory_ready_inbox_id=outcome.memory_ready_inbox_id,
            ingest_complete_inbox_id=outcome.ingest_complete_inbox_id,
        )
        if completed:
            self.client.ack(self.task_type, message.message_id)
        finished_at = datetime.now().astimezone()
        execution_ms = int((time.perf_counter() - execution_started) * 1000)
        log_event(
            "runtime.stream_task_completed",
            status="completed" if completed else "stale",
            space_id=str(task.get("space_id") or "") or None,
            message_id=task.get("source_message_id"),
            record_id=task_id,
            duration_ms=execution_ms,
            extra={
                **common_extra,
                "ownership_lost": ownership_lost.is_set() or not completed,
                "execution_ms": execution_ms,
                "total_duration_ms": _elapsed_ms(task.get("created_at"), finished_at),
            },
        )
