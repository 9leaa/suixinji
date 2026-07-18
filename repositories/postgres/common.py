"""Shared PostgreSQL repository helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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
) -> None:
    session.execute(
        insert(Tenant).values(id=tenant_id, name=tenant_id).on_conflict_do_nothing()
    )
    session.execute(
        insert(Space)
        .values(
            id=space_id,
            tenant_id=tenant_id,
            source=source,
            source_space_id=space_id,
            metadata_json=metadata or {},
        )
        .on_conflict_do_nothing()
    )


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
