"""Bounded task executor for ingest, query, and summary work."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

from agent.query_agent import answer_question
from core.file_lock import get_space_lock
from core.observability import log_event
from core.settings import MAX_WORKERS, TASK_QUEUE_SIZE
from core.worker import process_record
from runtime.delivery_store import (
    ingest_archived_key,
    manual_summary_key,
    mark_failed,
    mark_sent,
    mark_unknown,
    query_key,
    reserve_delivery,
)
from runtime.task import Task, create_task
from runtime.task_registry import TaskRegistry
from summary.daily_summary import generate_summary

LOGGER = logging.getLogger(__name__)
SendText = Callable[[str, str], bool]


class BoundedTaskExecutor:
    def __init__(
        self,
        *,
        max_workers: int = MAX_WORKERS,
        queue_size: int = TASK_QUEUE_SIZE,
        send_text: SendText | None = None,
    ) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="suixinji-task")
        self._slots = threading.BoundedSemaphore(max_workers + queue_size)
        self._max_workers = max_workers
        self._queue_size = queue_size
        self._capacity = max_workers + queue_size
        self._registry = TaskRegistry()
        self._send_text = send_text
        self._summary_locks: dict[str, threading.Lock] = {}
        self._summary_locks_guard = threading.Lock()
        self._inflight_ingest_keys: set[tuple[str, str]] = set()
        self._inflight_ingest_lock = threading.Lock()
        self._shutdown = False
        self._shutdown_lock = threading.Lock()

    def set_send_text(self, send_text: SendText) -> None:
        self._send_text = send_text

    def submit_ingest(
        self,
        record: Any,
        chat_id: str | None = None,
        *,
        notify_on_success: bool = False,
        source: str = "direct",
    ) -> Task:
        record_dict = _record_to_dict(record)
        message_id = str(record_dict.get("message_id") or "")
        task = create_task("ingest", record_dict["space_id"], {}, message_id=message_id or None)
        inflight_key = (task.space_id, message_id)
        reserved_inflight = False
        if message_id:
            reserved_inflight = self._reserve_inflight_ingest(inflight_key)
            if not reserved_inflight:
                return self._reject(task, "ingest already in flight")

        payload = {
            "record": record_dict,
            "chat_id": chat_id,
            "notify_on_success": notify_on_success,
            "source": source,
        }
        task.payload = payload
        submitted = self._submit(
            task,
            self._run_ingest,
            on_finished=lambda: self._release_inflight_ingest(inflight_key) if reserved_inflight else None,
        )
        if submitted.status == "rejected" and reserved_inflight:
            self._release_inflight_ingest(inflight_key)
        return submitted

    def submit_query(self, space_id: str, question: str, chat_id: str, message_id: str | None = None) -> Task:
        delivery_key = query_key(space_id, message_id) if message_id else query_key(space_id, "unknown")
        task = create_task(
            "query",
            space_id,
            {
                "question": question,
                "chat_id": chat_id,
                "delivery_key": delivery_key,
                "delivery_type": "query",
            },
            message_id=message_id,
        )
        return self._submit(task, self._run_query)

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
        if delivery_key is None and message_id:
            delivery_key = manual_summary_key(space_id, message_id)
        task = create_task(
            "summary",
            space_id,
            {
                "range_key": range_key,
                "chat_id": chat_id,
                "on_success": on_success,
                "delivery_key": delivery_key,
                "delivery_type": delivery_type or "manual_summary",
            },
            message_id=message_id,
        )
        return self._submit(task, self._run_summary)

    def get_stats(self) -> dict[str, Any]:
        stats = self._registry.get_stats()
        stats.update(
            {
                "max_workers": self._max_workers,
                "queue_size": self._queue_size,
                "capacity": self._capacity,
                "remaining_slots": self.remaining_slots(),
                "inflight_ingest": self.inflight_ingest_count(),
            }
        )
        return stats

    def remaining_slots(self) -> int:
        return int(getattr(self._slots, "_value", 0))

    def has_inflight_ingest(self, space_id: str, message_id: str) -> bool:
        with self._inflight_ingest_lock:
            return (space_id, message_id) in self._inflight_ingest_keys

    def inflight_ingest_count(self) -> int:
        with self._inflight_ingest_lock:
            return len(self._inflight_ingest_keys)

    def shutdown(self) -> None:
        with self._shutdown_lock:
            self._shutdown = True
        self._pool.shutdown(wait=True)

    def _submit(
        self,
        task: Task,
        runner: Callable[[Task], None],
        *,
        on_finished: Callable[[], None] | None = None,
    ) -> Task:
        with self._shutdown_lock:
            if self._shutdown:
                return self._reject(task, "executor is shutting down")

        if not self._slots.acquire(blocking=False):
            return self._reject(task, "task queue is full")

        self._registry.add(task)
        log_event(
            "runtime.task_queued",
            space_id=task.space_id,
            message_id=task.message_id,
            record_id=task.id,
            extra={"task_type": task.task_type},
        )
        self._pool.submit(self._run_task, task, runner, on_finished)
        return task

    def _reject(self, task: Task, error: str) -> Task:
        rejected = self._registry.reject(task, error)
        log_event(
            "runtime.task_rejected",
            level="warning",
            status="rejected",
            space_id=task.space_id,
            message_id=task.message_id,
            record_id=task.id,
            error=error,
            extra={"task_type": task.task_type},
        )
        return rejected

    def _run_task(self, task: Task, runner: Callable[[Task], None], on_finished: Callable[[], None] | None = None) -> None:
        self._registry.mark_running(task.id)
        log_event(
            "runtime.task_running",
            space_id=task.space_id,
            message_id=task.message_id,
            record_id=task.id,
            extra={"task_type": task.task_type},
        )
        try:
            runner(task)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("Task failed: type=%s id=%s", task.task_type, task.id)
            self._registry.mark_failed(task.id, error)
            log_event(
                "runtime.task_failed",
                level="error",
                status="failed",
                space_id=task.space_id,
                message_id=task.message_id,
                record_id=task.id,
                error=error,
                extra={"task_type": task.task_type, **_task_timing(task)},
            )
        else:
            self._registry.mark_success(task.id)
            log_event(
                "runtime.task_success",
                space_id=task.space_id,
                message_id=task.message_id,
                record_id=task.id,
                extra={"task_type": task.task_type, **_task_timing(task)},
            )
        finally:
            if on_finished is not None:
                on_finished()
            self._slots.release()

    def _run_ingest(self, task: Task) -> None:
        record = task.payload["record"]
        chat_id = task.payload.get("chat_id")
        with get_space_lock(task.space_id):
            process_record(record)
        if chat_id and task.payload.get("notify_on_success"):
            message_id = str(record.get("message_id") or task.message_id or task.id)
            self._deliver(
                chat_id,
                "已归档到随心记。",
                delivery_key=ingest_archived_key(task.space_id, message_id),
                delivery_type="ingest_archived",
                task=task,
            )

    def _run_query(self, task: Task) -> None:
        question = str(task.payload["question"])
        chat_id = str(task.payload["chat_id"])
        answer = answer_question(task.space_id, question)
        self._deliver(
            chat_id,
            answer,
            delivery_key=str(task.payload["delivery_key"]),
            delivery_type=str(task.payload["delivery_type"]),
            task=task,
        )

    def _run_summary(self, task: Task) -> None:
        range_key = str(task.payload["range_key"])
        chat_id = str(task.payload["chat_id"])
        lock = self._summary_lock(task.space_id)
        with lock:
            result = generate_summary(task.space_id, range_key)
            delivery_key = task.payload.get("delivery_key")
            if not delivery_key:
                delivery_key = manual_summary_key(task.space_id, task.message_id or task.id)
            self._deliver(
                chat_id,
                result.markdown,
                delivery_key=str(delivery_key),
                delivery_type=str(task.payload.get("delivery_type") or "manual_summary"),
                task=task,
                on_sent=task.payload.get("on_success"),
            )

    def _deliver(
        self,
        chat_id: str,
        text: str,
        *,
        delivery_key: str,
        delivery_type: str,
        task: Task,
        on_sent: Callable[[], None] | None = None,
    ) -> None:
        if self._send_text is None:
            return
        reserved = reserve_delivery(
            delivery_key,
            delivery_type=delivery_type,
            space_id=task.space_id,
            message_id=task.message_id,
        )
        if reserved is None:
            log_event(
                "runtime.delivery_skipped",
                status="skipped",
                space_id=task.space_id,
                message_id=task.message_id,
                record_id=task.id,
                extra={"delivery_key": delivery_key, "delivery_type": delivery_type},
            )
            return

        try:
            sent = self._send_text(chat_id, text)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if _looks_uncertain_send_error(exc):
                mark_unknown(delivery_key, error)
            else:
                mark_failed(delivery_key, error)
            raise

        if sent is False:
            error = "send_text returned False"
            mark_failed(delivery_key, error)
            raise RuntimeError(error)

        mark_sent(delivery_key)
        if on_sent is not None:
            on_sent()

    def _summary_lock(self, space_id: str) -> threading.Lock:
        with self._summary_locks_guard:
            lock = self._summary_locks.get(space_id)
            if lock is None:
                lock = threading.Lock()
                self._summary_locks[space_id] = lock
            return lock

    def _reserve_inflight_ingest(self, key: tuple[str, str]) -> bool:
        with self._inflight_ingest_lock:
            if key in self._inflight_ingest_keys:
                return False
            self._inflight_ingest_keys.add(key)
            return True

    def _release_inflight_ingest(self, key: tuple[str, str]) -> None:
        with self._inflight_ingest_lock:
            self._inflight_ingest_keys.discard(key)


def _record_to_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, dict):
        return dict(record)
    return asdict(record)


def _task_timing(task: Task) -> dict[str, int | None]:
    return {
        "queue_wait_ms": task.queue_wait_ms,
        "execution_ms": task.execution_ms,
        "total_duration_ms": task.total_duration_ms,
    }


def _looks_uncertain_send_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".casefold()
    return "timeout" in text or "timed out" in text or "connection" in text


_default_executor: BoundedTaskExecutor | None = None
_default_lock = threading.Lock()


def get_task_executor(send_text: SendText | None = None) -> BoundedTaskExecutor:
    global _default_executor
    with _default_lock:
        if _default_executor is None:
            _default_executor = BoundedTaskExecutor(send_text=send_text)
        elif send_text is not None:
            _default_executor.set_send_text(send_text)
        return _default_executor
