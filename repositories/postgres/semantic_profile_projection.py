"""Persistence helpers for semantic profile projections.

Projection rows are derived state.  They never alter source semantic memories.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from core.settings import (
    SEMANTIC_PROFILE_PROJECTION_MAX_AGE_SECONDS,
    SEMANTIC_PROFILE_PROJECTION_REBUILD_BATCH_SIZE,
)

from infrastructure.database import session_scope
from infrastructure.schema import Memory, SemanticProfileProjection
from memory.models import utc_now_iso
from repositories.postgres.common import parse_datetime


def _as_dict(row: SemanticProfileProjection) -> dict[str, Any]:
    return {
        "space_id": str(row.space_id),
        "facet": str(row.facet),
        "projection": dict(row.projection_json or {}),
        "source_memory_ids": [str(value) for value in list(row.source_memory_ids_json or [])],
        "processed_revision": int(row.processed_revision or 0),
        "target_revision": int(row.target_revision or 0),
        "status": str(row.status or "dirty"),
        "dirty_since": row.dirty_since.isoformat() if row.dirty_since else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_error": row.last_error,
    }


def mark_semantic_projection_dirty_in_session(session: Any, memory: Memory) -> int | None:
    """Advance one facet revision and atomically enqueue its rebuild task."""
    if memory.memory_type != "semantic" or memory.status != "active":
        return None
    facet = str(memory.predicate or "other").strip() or "other"
    now = parse_datetime(utc_now_iso())
    row = session.execute(
        select(SemanticProfileProjection)
        .where(
            SemanticProfileProjection.space_id == memory.space_id,
            SemanticProfileProjection.facet == facet,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        row = SemanticProfileProjection(
            space_id=memory.space_id,
            facet=facet,
            tenant_id=memory.tenant_id,
            projection_json={},
            source_memory_ids_json=[],
            processed_revision=0,
            target_revision=1,
            status="dirty",
            dirty_since=now,
            last_error=None,
        )
        session.add(row)
        revision = 1
    else:
        row.target_revision = int(row.target_revision or 0) + 1
        row.status = "dirty"
        row.dirty_since = row.dirty_since or now
        row.last_error = None
        revision = int(row.target_revision)
    session.flush()
    from repositories.postgres.dispatch import _enqueue_task_in_session

    _enqueue_task_in_session(
        session,
        task_type="semantic_profile_projection",
        tenant_id=str(memory.tenant_id),
        space_id=str(memory.space_id),
        source_message_id=None,
        idempotency_key=f"semantic-profile:{memory.space_id}:{facet}:{revision}",
        payload={"operation": "refresh", "facet": facet, "target_revision": revision},
        priority=-1,
        max_attempts=12,
        initial_status="queued",
        publish=True,
    )
    return revision


def get_semantic_profile_projections(space_id: str) -> dict[str, dict[str, Any]]:
    with session_scope() as session:
        rows = list(session.execute(
            select(SemanticProfileProjection)
            .where(SemanticProfileProjection.space_id == space_id)
            .order_by(SemanticProfileProjection.facet)
        ).scalars())
        return {str(row.facet): _as_dict(row) for row in rows}


def get_semantic_profile_projection(space_id: str, facet: str) -> dict[str, Any] | None:
    with session_scope() as session:
        row = session.execute(
            select(SemanticProfileProjection).where(
                SemanticProfileProjection.space_id == space_id,
                SemanticProfileProjection.facet == facet,
            )
        ).scalar_one_or_none()
        return _as_dict(row) if row is not None else None


def save_semantic_profile_projection(
    space_id: str,
    facet: str,
    *,
    expected_revision: int,
    projection: dict[str, Any],
    source_memory_ids: list[str],
) -> bool:
    """Persist only if a newer projection has not already won the race."""
    with session_scope() as session:
        row = session.execute(
            select(SemanticProfileProjection)
            .where(
                SemanticProfileProjection.space_id == space_id,
                SemanticProfileProjection.facet == facet,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None or int(row.processed_revision or 0) > int(expected_revision):
            return False
        row.projection_json = dict(projection)
        row.source_memory_ids_json = list(dict.fromkeys(str(value) for value in source_memory_ids if value))
        row.processed_revision = int(expected_revision)
        row.last_error = None
        if int(row.target_revision or 0) <= int(expected_revision):
            row.status = "fresh"
            row.dirty_since = None
        else:
            row.status = "dirty"
        return True


def record_semantic_profile_projection_error(space_id: str, facet: str, error: str) -> None:
    with session_scope() as session:
        row = session.execute(
            select(SemanticProfileProjection)
            .where(
                SemanticProfileProjection.space_id == space_id,
                SemanticProfileProjection.facet == facet,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is not None:
            row.status = "dirty"
            row.last_error = str(error)[:1000]
            row.dirty_since = row.dirty_since or parse_datetime(utc_now_iso())


def enqueue_stale_semantic_profile_projection_rebuilds(
    *,
    limit: int = SEMANTIC_PROFILE_PROJECTION_REBUILD_BATCH_SIZE,
) -> int:
    """Mark expired fresh projections dirty and enqueue one rebuild per facet."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=max(60, int(SEMANTIC_PROFILE_PROJECTION_MAX_AGE_SECONDS))
    )
    now = parse_datetime(utc_now_iso())
    queued = 0
    with session_scope() as session:
        rows = list(session.execute(
            select(SemanticProfileProjection)
            .where(
                SemanticProfileProjection.status == "fresh",
                SemanticProfileProjection.updated_at < cutoff,
            )
            .order_by(SemanticProfileProjection.updated_at)
            .limit(max(1, int(limit)))
            .with_for_update(skip_locked=True)
        ).scalars())
        if not rows:
            return 0
        from repositories.postgres.dispatch import _enqueue_task_in_session

        for row in rows:
            row.target_revision = int(row.target_revision or 0) + 1
            row.status = "dirty"
            row.dirty_since = now
            row.last_error = None
            revision = int(row.target_revision)
            _enqueue_task_in_session(
                session,
                task_type="semantic_profile_projection",
                tenant_id=str(row.tenant_id),
                space_id=str(row.space_id),
                source_message_id=None,
                idempotency_key=f"semantic-profile:{row.space_id}:{row.facet}:{revision}",
                payload={"operation": "refresh", "facet": str(row.facet), "target_revision": revision},
                priority=-1,
                max_attempts=12,
                initial_status="queued",
                publish=True,
            )
            queued += 1
    return queued


def enqueue_uninitialized_semantic_profile_projection_rebuilds(
    *,
    limit: int = SEMANTIC_PROFILE_PROJECTION_REBUILD_BATCH_SIZE,
) -> int:
    """Publish backfilled dirty facets that predate the projection feature."""
    queued = 0
    with session_scope() as session:
        rows = list(session.execute(
            select(SemanticProfileProjection)
            .where(
                SemanticProfileProjection.status == "dirty",
                SemanticProfileProjection.processed_revision == 0,
            )
            .order_by(SemanticProfileProjection.dirty_since, SemanticProfileProjection.space_id)
            .limit(max(1, int(limit)))
            .with_for_update(skip_locked=True)
        ).scalars())
        if not rows:
            return 0
        from repositories.postgres.dispatch import _enqueue_task_in_session

        for row in rows:
            revision = int(row.target_revision or 1)
            _enqueue_task_in_session(
                session,
                task_type="semantic_profile_projection",
                tenant_id=str(row.tenant_id),
                space_id=str(row.space_id),
                source_message_id=None,
                idempotency_key=f"semantic-profile:{row.space_id}:{row.facet}:{revision}",
                payload={"operation": "refresh", "facet": str(row.facet), "target_revision": revision},
                priority=-1,
                max_attempts=12,
                initial_status="queued",
                publish=True,
            )
            queued += 1
    return queued
