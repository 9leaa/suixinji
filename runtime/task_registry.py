"""In-memory task registry and runtime counters."""

from __future__ import annotations

import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from core.settings import TASK_HISTORY_LIMIT, TASK_HISTORY_TTL_HOURS
from runtime.task import TASK_FAILED, TASK_QUEUED, TASK_REJECTED, TASK_RUNNING, TASK_SUCCESS, Task, now_iso


class TaskRegistry:
    def __init__(
        self,
        *,
        history_limit: int = TASK_HISTORY_LIMIT,
        history_ttl_hours: int = TASK_HISTORY_TTL_HOURS,
    ) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._history_limit = history_limit
        self._history_ttl_hours = history_ttl_hours
        self._success_count = 0
        self._failed_count = 0
        self._rejected_count = 0
        self._last_llm_timeout_at: str | None = None
        self._last_llm_timeout_error: str | None = None

    def add(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.id] = task
        return task

    def reject(self, task: Task, error: str) -> Task:
        with self._lock:
            task.status = TASK_REJECTED
            task.finished_at = now_iso()
            task.total_duration_ms = _duration_ms(task.created_at, task.finished_at)
            task.error = error
            self._tasks[task.id] = task
            self._rejected_count += 1
            self._prune_finished_tasks()
        return task

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = TASK_RUNNING
            task.started_at = now_iso()
            task.queue_wait_ms = _duration_ms(task.created_at, task.started_at)

    def mark_success(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = TASK_SUCCESS
            task.finished_at = now_iso()
            task.execution_ms = _duration_ms(task.started_at, task.finished_at)
            task.total_duration_ms = _duration_ms(task.created_at, task.finished_at)
            self._success_count += 1
            self._prune_finished_tasks()

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = TASK_FAILED
            task.finished_at = now_iso()
            task.execution_ms = _duration_ms(task.started_at, task.finished_at)
            task.total_duration_ms = _duration_ms(task.created_at, task.finished_at)
            task.error = error
            self._failed_count += 1
            if _looks_like_timeout(error):
                self._last_llm_timeout_at = task.finished_at
                self._last_llm_timeout_error = error
            self._prune_finished_tasks()

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            queued = [task for task in self._tasks.values() if task.status == TASK_QUEUED]
            running = [task for task in self._tasks.values() if task.status == TASK_RUNNING]
            oldest_wait = 0
            if queued:
                created_at = min(task.created_at for task in queued)
                oldest_wait = _age_seconds(created_at)

            return {
                "running": len(running),
                "queued": len(queued),
                "success": self._success_count,
                "failed": self._failed_count,
                "rejected": self._rejected_count,
                "oldest_queued_wait_seconds": oldest_wait,
                "last_llm_timeout_at": self._last_llm_timeout_at,
                "last_llm_timeout_error": self._last_llm_timeout_error,
                "retained_tasks": len(self._tasks),
                "recent_tasks": [asdict(task) for task in list(self._tasks.values())[-10:]],
            }

    def retained_count(self) -> int:
        with self._lock:
            return len(self._tasks)

    def _prune_finished_tasks(self) -> None:
        protected = {
            task_id
            for task_id, task in self._tasks.items()
            if task.status in {TASK_QUEUED, TASK_RUNNING}
        }
        finished = [
            (task_id, task)
            for task_id, task in self._tasks.items()
            if task_id not in protected
        ]
        if not finished:
            return

        now = datetime.now().astimezone()
        failed_cutoff = now - timedelta(hours=self._history_ttl_hours)
        keep_failed = {
            task_id
            for task_id, task in finished
            if task.status == TASK_FAILED and _parse_iso(task.finished_at) >= failed_cutoff
        }
        ordered = sorted(
            finished,
            key=lambda item: item[1].finished_at or item[1].created_at,
            reverse=True,
        )
        keep_recent = {task_id for task_id, _task in ordered[: self._history_limit]}
        keep = protected | keep_failed | keep_recent
        self._tasks = {
            task_id: task
            for task_id, task in self._tasks.items()
            if task_id in keep
        }


def _age_seconds(value: str) -> int:
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        return 0
    return max(0, int((datetime.now().astimezone() - created).total_seconds()))


def _duration_ms(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        return max(0, int((_parse_iso(end) - _parse_iso(start)).total_seconds() * 1000))
    except ValueError:
        return None


def _parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _looks_like_timeout(error: str) -> bool:
    lowered = error.casefold()
    return "timeout" in lowered or "timed out" in lowered or "超时" in lowered
