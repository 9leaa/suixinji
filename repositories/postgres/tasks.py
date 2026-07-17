"""PostgreSQL task and attempt persistence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import Task
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime


def create_task(task: dict[str, Any]) -> bool:
    space_id = str(task["space_id"])
    tenant_id = str(task.get("tenant_id") or DEFAULT_TENANT_ID)
    with session_scope() as session:
        ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        created = session.execute(
            insert(Task)
            .values(
                id=str(task["id"]),
                task_type=str(task["task_type"]),
                tenant_id=tenant_id,
                space_id=space_id,
                source_message_id=task.get("source_message_id"),
                idempotency_key=str(task["idempotency_key"]),
                status=str(task.get("status") or "queued"),
                priority=int(task.get("priority") or 0),
                attempt_count=int(task.get("attempt_count") or 0),
                max_attempts=int(task.get("max_attempts") or 3),
                payload_json=dict(task.get("payload") or task.get("payload_json") or {}),
                next_retry_at=parse_datetime(task["next_retry_at"]) if task.get("next_retry_at") else None,
            )
            .on_conflict_do_nothing(index_elements=[Task.idempotency_key])
            .returning(Task.id)
        ).scalar_one_or_none()
        return created is not None


def get_task(task_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        row = session.get(Task, task_id)
        if row is None:
            return None
        return {column.name: getattr(row, column.name) for column in Task.__table__.columns}


def update_task_status(task_id: str, status: str, **updates: Any) -> None:
    allowed = {"attempt_count", "next_retry_at", "started_at", "completed_at", "last_error"}
    values = {key: value for key, value in updates.items() if key in allowed}
    values["status"] = status
    with session_scope() as session:
        session.execute(update(Task).where(Task.id == task_id).values(**values))
