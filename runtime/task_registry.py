"""In-memory task registry and runtime counters."""

from __future__ import annotations

import threading
from dataclasses import asdict
from datetime import datetime
from typing import Any

from runtime.task import TASK_FAILED, TASK_QUEUED, TASK_REJECTED, TASK_RUNNING, TASK_SUCCESS, Task, now_iso


class TaskRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
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
            task.error = error
            self._tasks[task.id] = task
            self._rejected_count += 1
        return task

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = TASK_RUNNING
            task.started_at = now_iso()

    def mark_success(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = TASK_SUCCESS
            task.finished_at = now_iso()
            self._success_count += 1

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = TASK_FAILED
            task.finished_at = now_iso()
            task.error = error
            self._failed_count += 1
            if _looks_like_timeout(error):
                self._last_llm_timeout_at = task.finished_at
                self._last_llm_timeout_error = error

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
                "recent_tasks": [asdict(task) for task in list(self._tasks.values())[-10:]],
            }


def _age_seconds(value: str) -> int:
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        return 0
    return max(0, int((datetime.now().astimezone() - created).total_seconds()))


def _looks_like_timeout(error: str) -> bool:
    lowered = error.casefold()
    return "timeout" in lowered or "timed out" in lowered or "超时" in lowered
