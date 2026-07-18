"""Shared PostgreSQL repository helpers."""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from infrastructure.schema import Space, Tenant, User

DEFAULT_TENANT_ID = "default"


def parse_datetime(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value
    if value:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed
    return datetime.now().astimezone()


def ensure_tenant_space(
    session: Session,
    space_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    source: str = "feishu",
    metadata: dict[str, Any] | None = None,
) -> str:
    source_space_id = str(space_id)
    internal_existing = session.get(Space, source_space_id)
    if internal_existing is not None and internal_existing.tenant_id == tenant_id:
        return str(internal_existing.id)
    session.execute(
        insert(Tenant).values(id=tenant_id, name=tenant_id).on_conflict_do_nothing()
    )
    existing = session.execute(
        select(Space.id).where(
            Space.tenant_id == tenant_id,
            Space.source == source,
            Space.source_space_id == source_space_id,
        )
    ).scalar_one_or_none()
    if existing:
        return str(existing)

    def _insert(preferred_id: str) -> str | None:
        return session.execute(
            insert(Space)
            .values(
                id=preferred_id,
                tenant_id=tenant_id,
                source=source,
                source_space_id=source_space_id,
                metadata_json=metadata or {},
            )
            .on_conflict_do_nothing()
            .returning(Space.id)
        ).scalar_one_or_none()

    created = _insert(source_space_id)
    if created:
        return str(created)

    digest = hashlib.sha256(f"{tenant_id}:{source}:{source_space_id}".encode("utf-8")).hexdigest()[:32]
    internal_id = f"space_{digest}"
    created = _insert(internal_id)
    if created:
        return str(created)

    existing = session.execute(
        select(Space.id).where(
            Space.tenant_id == tenant_id,
            Space.source == source,
            Space.source_space_id == source_space_id,
        )
    ).scalar_one()
    return str(existing)


def ensure_user(
    session: Session,
    user_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    source: str = "feishu",
    profile: dict[str, Any] | None = None,
) -> None:
    session.execute(
        insert(Tenant).values(id=tenant_id, name=tenant_id).on_conflict_do_nothing()
    )
    session.execute(
        insert(User)
        .values(
            id=user_id,
            tenant_id=tenant_id,
            source=source,
            source_user_id=user_id,
            profile_json=profile or {},
        )
        .on_conflict_do_nothing()
    )
