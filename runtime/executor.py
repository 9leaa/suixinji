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
from runtime.retry import run_with_retries
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
        self._registry = TaskRegistry()
        self._send_text = send_text
        self._summary_locks: dict[str, threading.Lock] = {}
        self._summary_locks_guard = threading.Lock()
        self._shutdown = False
        self._shutdown_lock = threading.Lock()

    def set_send_text(self, send_text: SendText) -> None:
        self._send_text = send_text

    def submit_ingest(self, record: Any, chat_id: str | None = None) -> Task:
        payload = {"record": _record_to_dict(record), "chat_id": chat_id}
        task = create_task("ingest", payload["record"]["space_id"], payload, message_id=payload["record"].get("message_id"))
        return self._submit(task, self._run_ingest)

    def submit_query(self, space_id: str, question: str, chat_id: str, message_id: str | None = None) -> Task:
        task = create_task(
            "query",
            space_id,
            {"question": question, "chat_id": chat_id},
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
    ) -> Task:
        task = create_task(
            "summary",
            space_id,
            {"range_key": range_key, "chat_id": chat_id, "on_success": on_success},
            message_id=message_id,
        )
        return self._submit(task, self._run_summary)

    def get_stats(self) -> dict[str, Any]:
        return self._registry.get_stats()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            self._shutdown = True
        self._pool.shutdown(wait=True)

    def _submit(self, task: Task, runner: Callable[[Task], None]) -> Task:
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
        self._pool.submit(self._run_task, task, runner)
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

    def _run_task(self, task: Task, runner: Callable[[Task], None]) -> None:
        self._registry.mark_running(task.id)
        log_event(
            "runtime.task_running",
            space_id=task.space_id,
            message_id=task.message_id,
            record_id=task.id,
            extra={"task_type": task.task_type},
        )
        try:
            run_with_retries(lambda: runner(task))
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
                extra={"task_type": task.task_type},
            )
        else:
            self._registry.mark_success(task.id)
            log_event(
                "runtime.task_success",
                space_id=task.space_id,
                message_id=task.message_id,
                record_id=task.id,
                extra={"task_type": task.task_type},
            )
        finally:
            self._slots.release()

    def _run_ingest(self, task: Task) -> None:
        record = task.payload["record"]
        chat_id = task.payload.get("chat_id")
        with get_space_lock(task.space_id):
            process_record(record)
        if chat_id:
            self._safe_send(chat_id, "已归档到随心记。")

    def _run_query(self, task: Task) -> None:
        question = str(task.payload["question"])
        chat_id = str(task.payload["chat_id"])
        answer = answer_question(task.space_id, question)
        self._safe_send(chat_id, answer)

    def _run_summary(self, task: Task) -> None:
        range_key = str(task.payload["range_key"])
        chat_id = str(task.payload["chat_id"])
        lock = self._summary_lock(task.space_id)
        with lock:
            result = generate_summary(task.space_id, range_key)
            self._safe_send(chat_id, result.markdown)
            on_success = task.payload.get("on_success")
            if on_success is not None:
                on_success()

    def _safe_send(self, chat_id: str, text: str) -> None:
        if self._send_text is None:
            return
        sent = self._send_text(chat_id, text)
        if sent is False:
            raise RuntimeError("send_text returned False")

    def _summary_lock(self, space_id: str) -> threading.Lock:
        with self._summary_locks_guard:
            lock = self._summary_locks.get(space_id)
            if lock is None:
                lock = threading.Lock()
                self._summary_locks[space_id] = lock
            return lock


def _record_to_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, dict):
        return dict(record)
    return asdict(record)


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
