"""文件作用：本地任务注册表。

项目关系：本文件依赖 `core.settings`、`runtime.task`；被 `runtime.executor`、`tests.test_task_registry_retention`。
"""



from __future__ import annotations

import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from core.settings import TASK_HISTORY_LIMIT, TASK_HISTORY_TTL_HOURS
from runtime.task import TASK_FAILED, TASK_QUEUED, TASK_REJECTED, TASK_RUNNING, TASK_SUCCESS, Task, now_iso


class TaskRegistry:
    """类功能：`TaskRegistry` 封装与“本地任务注册表”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(
        self,
        *,
        history_limit: int = TASK_HISTORY_LIMIT,
        history_ttl_hours: int = TASK_HISTORY_TTL_HOURS,
    ) -> None:
        """函数功能：`TaskRegistry.__init__` 在类 `TaskRegistry` 中负责初始化实例状态，服务于本文件职责：本地任务注册表。
        传参：
            history_limit: history limit 参数，由调用方传入，类型为 `int`，默认值为 `TASK_HISTORY_LIMIT`。
            history_ttl_hours: history ttl hours 参数，由调用方传入，类型为 `int`，默认值为 `TASK_HISTORY_TTL_HOURS`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
        """函数功能：`TaskRegistry.add` 在类 `TaskRegistry` 中负责处理 add，服务于本文件职责：本地任务注册表。
        传参：
            task: task 参数，由调用方传入，类型为 `Task`。
        返回结果说明：
            返回 `Task` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        with self._lock:
            self._tasks[task.id] = task
        return task

    def reject(self, task: Task, error: str) -> Task:
        """函数功能：`TaskRegistry.reject` 在类 `TaskRegistry` 中负责拒绝，服务于本文件职责：本地任务注册表。
        传参：
            task: task 参数，由调用方传入，类型为 `Task`。
            error: 当前捕获的异常对象，类型为 `str`。
        返回结果说明：
            返回 `Task` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
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
        """函数功能：`TaskRegistry.mark_running` 在类 `TaskRegistry` 中负责标记 running，服务于本文件职责：本地任务注册表。
        传参：
            task_id: 任务标识，用于查询、更新或幂等处理任务状态，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._lock:
            task = self._tasks[task_id]
            task.status = TASK_RUNNING
            task.started_at = now_iso()
            task.queue_wait_ms = _duration_ms(task.created_at, task.started_at)

    def mark_success(self, task_id: str) -> None:
        """函数功能：`TaskRegistry.mark_success` 在类 `TaskRegistry` 中负责标记 success，服务于本文件职责：本地任务注册表。
        传参：
            task_id: 任务标识，用于查询、更新或幂等处理任务状态，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._lock:
            task = self._tasks[task_id]
            task.status = TASK_SUCCESS
            task.finished_at = now_iso()
            task.execution_ms = _duration_ms(task.started_at, task.finished_at)
            task.total_duration_ms = _duration_ms(task.created_at, task.finished_at)
            self._success_count += 1
            self._prune_finished_tasks()

    def mark_failed(self, task_id: str, error: str) -> None:
        """函数功能：`TaskRegistry.mark_failed` 在类 `TaskRegistry` 中负责标记 failed，服务于本文件职责：本地任务注册表。
        传参：
            task_id: 任务标识，用于查询、更新或幂等处理任务状态，类型为 `str`。
            error: 当前捕获的异常对象，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
        """函数功能：`TaskRegistry.get_stats` 在类 `TaskRegistry` 中负责获取 stats，服务于本文件职责：本地任务注册表。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
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
        """函数功能：`TaskRegistry.retained_count` 在类 `TaskRegistry` 中负责计数 retained，服务于本文件职责：本地任务注册表。
        传参：
            无。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
        with self._lock:
            return len(self._tasks)

    def _prune_finished_tasks(self) -> None:
        """函数功能：`TaskRegistry._prune_finished_tasks` 在类 `TaskRegistry` 中负责处理 prune finished tasks，服务于本文件职责：本地任务注册表。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
    """函数功能：`_age_seconds` 负责处理 age seconds，服务于本文件职责：本地任务注册表。
    传参：
        value: 待转换、校验或计算的值，类型为 `str`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        return 0
    return max(0, int((datetime.now().astimezone() - created).total_seconds()))


def _duration_ms(start: str | None, end: str | None) -> int | None:
    """函数功能：`_duration_ms` 负责处理 duration ms，服务于本文件职责：本地任务注册表。
    传参：
        start: start 参数，由调用方传入，类型为 `str | None`。
        end: end 参数，由调用方传入，类型为 `str | None`。
    返回结果说明：
        返回 `int | None`；未命中或无需处理时可返回 `None`。
    """
    if not start or not end:
        return None
    try:
        return max(0, int((_parse_iso(end) - _parse_iso(start)).total_seconds() * 1000))
    except ValueError:
        return None


def _parse_iso(value: str | None) -> datetime:
    """函数功能：`_parse_iso` 负责解析 iso，服务于本文件职责：本地任务注册表。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
    返回结果说明：
        返回 `datetime` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if not value:
        return datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _looks_like_timeout(error: str) -> bool:
    """函数功能：`_looks_like_timeout` 负责处理 looks like timeout，服务于本文件职责：本地任务注册表。
    传参：
        error: 当前捕获的异常对象，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    lowered = error.casefold()
    return "timeout" in lowered or "timed out" in lowered or "超时" in lowered
