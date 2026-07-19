"""Generic Redis Streams worker with durable PostgreSQL task state."""

from __future__ import annotations

import logging
import hashlib
import os
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
    PROCESS_ROLE,
    STREAM_CLAIM_IDLE_MS,
    STREAM_RECLAIM_INTERVAL_SECONDS,
    TASK_LEASE_SECONDS,
    WORKER_RETRY_BASE_SECONDS,
)
from repositories.postgres.tasks import claim_task, complete_task, defer_task, fail_task, get_task, renew_task_lease
from runtime.streams.client import GROUPS, StreamClient, StreamMessage

LOGGER = logging.getLogger(__name__)
HEARTBEAT_SESSION_ROLE = "worker-heartbeat"


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


def _safe_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _lease_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


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
        if not messages:
            messages = self._reclaim_if_due()
        for message in messages:
            self._handle(message)
        return len(messages)

    def _reclaim_if_due(self) -> list[StreamMessage]:
        now = time.monotonic()
        if now < self._next_reclaim_at:
            return []
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
        if messages:
            log_event(
                "runtime.stream_pending_reclaimed",
                status="reclaimed",
                extra={
                    "task_type": self.task_type,
                    "worker_id": self.worker_id,
                    "reclaim_count": len(messages),
                    "next_start_id": self.client.reclaim_cursor(self.task_type, self.worker_id),
                    "min_idle_ms": STREAM_CLAIM_IDLE_MS,
                    "redis_message_ids": [message.message_id for message in messages],
                },
            )
        return messages

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
        stream_extra = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_role": PROCESS_ROLE,
            "worker_id": self.worker_id,
            "task_id": task_id or None,
            "task_type": self.task_type,
            "stream": message.stream,
            "consumer_group": GROUPS.get(self.task_type),
            "consumer": self.worker_id,
            "redis_message_id": message.message_id,
        }
        log_event(
            "runtime.stream_message_received",
            status="received",
            record_id=task_id or None,
            extra=stream_extra,
        )
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
            log_event(
                "runtime.task_claim_skipped",
                status=str((existing or {}).get("status") or "missing"),
                record_id=task_id,
                extra={**stream_extra, "previous_status": (existing or {}).get("status")},
            )
            return
        lease_token = str(task["lease_token"])
        claim_version = int(task["claim_version"])
        stop_heartbeat = threading.Event()
        ownership_lost = threading.Event()

        def renew_lease() -> None:
            interval = max(0.5, TASK_LEASE_SECONDS / 3)
            while not stop_heartbeat.wait(interval):
                renew_started = time.perf_counter()
                try:
                    renewed = renew_task_lease(
                        task_id,
                        lease_token=lease_token,
                        claim_version=claim_version,
                        lease_seconds=TASK_LEASE_SECONDS,
                        session_role=HEARTBEAT_SESSION_ROLE,
                    )
                    if renewed:
                        log_event(
                            "runtime.task_lease_renewed",
                            status="success",
                            space_id=str(task.get("space_id") or "") or None,
                            message_id=task.get("source_message_id"),
                            record_id=task_id,
                            duration_ms=int((time.perf_counter() - renew_started) * 1000),
                            extra=common_extra,
                        )
                    else:
                        ownership_lost.set()
                        log_event(
                            "runtime.task_lease_renew_failed",
                            level="warning",
                            status="stale",
                            space_id=str(task.get("space_id") or "") or None,
                            message_id=task.get("source_message_id"),
                            record_id=task_id,
                            duration_ms=int((time.perf_counter() - renew_started) * 1000),
                            extra=common_extra,
                        )
                        return
                except Exception as exc:
                    log_event(
                        "runtime.task_lease_renew_failed",
                        level="warning",
                        status="failed",
                        space_id=str(task.get("space_id") or "") or None,
                        message_id=task.get("source_message_id"),
                        record_id=task_id,
                        duration_ms=int((time.perf_counter() - renew_started) * 1000),
                        error=type(exc).__name__,
                        extra=common_extra,
                    )
                    LOGGER.warning("task lease renewal failed: task_id=%s", task_id, exc_info=True)

        execution_started = time.perf_counter()
        queue_wait_ms = _elapsed_ms(task.get("created_at"), task.get("started_at"))
        common_extra = {
            **stream_extra,
            "task_id": task_id,
            "task_type": self.task_type,
            "worker_id": self.worker_id,
            "attempt_count": int(task.get("attempt_count") or 0),
            "failure_count": int(task.get("failure_count") or 0),
            "defer_count": int(task.get("defer_count") or 0),
            "queue_wait_ms": queue_wait_ms,
            "claim_version": claim_version,
            "lease_token_hash": _lease_hash(lease_token),
            "lease_expires_at": _safe_iso(task.get("lease_expires_at")),
            "previous_status": task.get("previous_status"),
            "previous_claimed_by": task.get("previous_claimed_by"),
            "previous_lease_expires_at": _safe_iso(task.get("previous_lease_expires_at")),
        }
        log_event(
            "runtime.task_claimed",
            status="running",
            space_id=str(task.get("space_id") or "") or None,
            message_id=task.get("source_message_id"),
            record_id=task_id,
            extra=common_extra,
        )
        if task.get("previous_status") == "running":
            log_event(
                "runtime.task_lease_reclaimed",
                status="reclaimed",
                space_id=str(task.get("space_id") or "") or None,
                message_id=task.get("source_message_id"),
                record_id=task_id,
                extra={**common_extra, "reclaim_reason": "lease_expired"},
            )
        log_event(
            "runtime.stream_task_started",
            status="running",
            space_id=str(task.get("space_id") or "") or None,
            message_id=task.get("source_message_id"),
            record_id=task_id,
            extra=common_extra,
        )

        heartbeat = threading.Thread(target=renew_lease, name=f"task-lease-{task_id[-8:]}", daemon=True)
        heartbeat.start()

        def finish_heartbeat() -> None:
            stop_heartbeat.set()
            heartbeat.join(timeout=1)

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
            log_event(
                "runtime.task_retry_scheduled",
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
                    "reclaim_reason": "handler_deferred",
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
            log_event(
                "runtime.task_failed",
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
            if status == "retry":
                log_event(
                    "runtime.task_retry_scheduled",
                    level="warning",
                    status="retry",
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
                        "reclaim_reason": "handler_failed",
                    },
                )
            elif status == "dead_letter":
                log_event(
                    "runtime.task_dead_lettered",
                    level="error",
                    status="dead_letter",
                    space_id=str(task.get("space_id") or "") or None,
                    message_id=task.get("source_message_id"),
                    record_id=task_id,
                    duration_ms=execution_ms,
                    error=type(exc).__name__,
                    extra={
                        **common_extra,
                        "failure_count": failure_no,
                        "execution_ms": execution_ms,
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
        log_event(
            "runtime.task_completed" if completed else "runtime.task_stale_completion",
            level="info" if completed else "warning",
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


class AdaptiveStreamWorker:
    """Share one process and DB connection budget across all task streams."""

    def __init__(
        self,
        handlers: dict[str, TaskHandler],
        *,
        client: StreamClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.client = client or StreamClient()
        self.worker_id = worker_id or f"{socket.gethostname()}-adaptive-{uuid.uuid4().hex[:8]}"
        self.task_types = list(handlers)
        if not self.task_types:
            raise ValueError("adaptive worker requires at least one task handler")
        self.workers = {
            task_type: StreamWorker(
                task_type,
                handler,
                client=self.client,
                worker_id=self.worker_id,
            )
            for task_type, handler in handlers.items()
        }
        self.running = True
        self.foreground_task_types = [
            task_type for task_type in self.task_types if task_type not in {"delivery", "enrichment"}
        ]
        self.background_task_types = [
            task_type for task_type in self.task_types if task_type in {"delivery", "enrichment"}
        ]
        self._foreground_cursor = 0
        self._background_cursor = 0
        self._foreground_batches = 0

    @staticmethod
    def _rotated(task_types: list[str], cursor: int) -> list[str]:
        return task_types[cursor:] + task_types[:cursor]

    def _handle_messages(self, messages: list[StreamMessage]) -> None:
        for message in messages:
            task_type = str(message.fields.get("task_type") or "")
            worker = self.workers.get(task_type)
            if worker is None:
                self.client.dead_letter(message, error=f"unsupported task_type: {task_type}")
                continue
            worker._handle(message)

    def run_once(self) -> int:
        foreground = self._rotated(self.foreground_task_types, self._foreground_cursor)
        background = self._rotated(self.background_task_types, self._background_cursor)
        lanes = [(foreground, True), (background, False)]
        if background and self._foreground_batches >= 4:
            lanes.reverse()
        for lane, is_foreground in lanes:
            if not lane:
                continue
            messages = self.client.read_many(lane, self.worker_id, count=1)
            if not messages:
                continue
            self._handle_messages(messages)
            if is_foreground:
                self._foreground_cursor = (self._foreground_cursor + 1) % len(self.foreground_task_types)
                self._foreground_batches += 1
            else:
                self._background_cursor = (self._background_cursor + 1) % len(self.background_task_types)
                self._foreground_batches = 0
            return len(messages)
        ordered = self._rotated(self.task_types, self._foreground_cursor % len(self.task_types))
        for task_type in ordered:
            reclaimed = self.workers[task_type]._reclaim_if_due()
            if not reclaimed:
                continue
            for message in reclaimed:
                self.workers[task_type]._handle(message)
            return len(reclaimed)
        return 0

    def run_forever(self) -> None:
        idle_sleep = 0.02
        while self.running:
            try:
                if self.run_once() == 0:
                    time.sleep(idle_sleep)
                    idle_sleep = min(0.25, idle_sleep * 2)
                else:
                    idle_sleep = 0.02
            except Exception:
                LOGGER.exception("adaptive stream worker loop failed: worker=%s", self.worker_id)
                idle_sleep = 0.02
                time.sleep(1)

    def stop(self) -> None:
        self.running = False
