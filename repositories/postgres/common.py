"""文件作用：PostgreSQL 公共 helpers。

项目关系：本文件依赖 `infrastructure.schema`；被 `eval.benchmark_stage2_queries`、`repositories.postgres.agent_runs`、`repositories.postgres.delivery`、`repositories.postgres.dispatch` 等 13 个模块。
"""



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
    """函数功能：`parse_datetime` 负责解析 datetime，服务于本文件职责：PostgreSQL 公共 helpers。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | datetime | None`。
    返回结果说明：
        返回 `datetime` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
    """函数功能：`ensure_tenant_space` 负责确保 tenant space，服务于本文件职责：PostgreSQL 公共 helpers。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Session`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`，默认值为 `DEFAULT_TENANT_ID`。
        source: source 参数，由调用方传入，类型为 `str`，默认值为 `'feishu'`。
        metadata: metadata 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
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
        """函数功能：`_insert` 负责处理 insert，服务于本文件职责：PostgreSQL 公共 helpers。
        传参：
            preferred_id: preferred id 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `str | None`；未命中或无需处理时可返回 `None`。
        """
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
    """函数功能：`ensure_user` 负责确保 user，服务于本文件职责：PostgreSQL 公共 helpers。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Session`。
        user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`，默认值为 `DEFAULT_TENANT_ID`。
        source: source 参数，由调用方传入，类型为 `str`，默认值为 `'feishu'`。
        profile: profile 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
