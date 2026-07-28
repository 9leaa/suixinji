"""Task data structures used by the bounded executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4


TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_SUCCESS = "success"
TASK_FAILED = "failed"
TASK_REJECTED = "rejected"


def now_iso() -> str:
    """负责“nowiso”。

    该函数是 `runtime.task` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass
class Task:
    id: str
    task_type: str
    space_id: str
    message_id: str | None
    payload: dict[str, Any]
    status: str = TASK_QUEUED
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    queue_wait_ms: int | None = None
    execution_ms: int | None = None
    total_duration_ms: int | None = None


def create_task(
    task_type: str,
    space_id: str,
    payload: dict[str, Any],
    *,
    message_id: str | None = None,
    status: str = TASK_QUEUED,
) -> Task:
    """负责“创建任务”。

    该函数是 `runtime.task` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return Task(
        id=str(uuid4()),
        task_type=task_type,
        space_id=space_id,
        message_id=message_id,
        payload=payload,
        status=status,
    )
