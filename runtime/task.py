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
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def create_task(
    task_type: str,
    space_id: str,
    payload: dict[str, Any],
    *,
    message_id: str | None = None,
    status: str = TASK_QUEUED,
) -> Task:
    return Task(
        id=str(uuid4()),
        task_type=task_type,
        space_id=space_id,
        message_id=message_id,
        payload=payload,
        status=status,
    )
