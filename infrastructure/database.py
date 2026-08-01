"""文件作用：SQLAlchemy engine/session 生命周期。

项目关系：本文件依赖 `core.settings`；被 `alembic.env`、`eval.benchmark_stage2_queries`、`eval.large_live_retrieval_eval`、`eval.live_retrieval_eval` 等 28 个模块。
"""



from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from core.settings import (
    DATABASE_POOL_RECYCLE_SECONDS,
    DATABASE_POOL_TIMEOUT_SECONDS,
    DATABASE_URL,
    PROCESS_ROLE,
    SUIXINJI_ENV,
    database_pool_budget,
)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_role_engines: dict[str, Engine] = {}
_role_session_factories: dict[str, sessionmaker[Session]] = {}


def _normalized_database_url(url: str) -> str:
    """函数功能：`_normalized_database_url` 负责处理 normalized database url，服务于本文件职责：SQLAlchemy engine/session 生命周期。
    传参：
        url: url 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _resolved_role(role: str | None = None) -> str:
    """函数功能：`_resolved_role` 负责处理 resolved role，服务于本文件职责：SQLAlchemy engine/session 生命周期。
    传参：
        role: role 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return (role or PROCESS_ROLE or "default").strip().lower() or "default"


def get_engine(role: str | None = None) -> Engine:
    """函数功能：`get_engine` 负责获取 engine，服务于本文件职责：SQLAlchemy engine/session 生命周期。
    传参：
        role: role 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `Engine` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    global _engine, _session_factory
    resolved = _resolved_role(role)
    default_role = _resolved_role(None)
    if resolved == default_role:
        if _engine is not None:
            return _engine
    elif resolved in _role_engines:
        return _role_engines[resolved]
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    pool_size, max_overflow = database_pool_budget(resolved if role is not None else None)
    engine = create_engine(
        _normalized_database_url(DATABASE_URL),
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=max(1, DATABASE_POOL_TIMEOUT_SECONDS),
        pool_recycle=max(60, DATABASE_POOL_RECYCLE_SECONDS),
        connect_args={"application_name": f"suixinji:{SUIXINJI_ENV}:{resolved}:{os.getpid()}"},
        future=True,
    )
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    if resolved == default_role:
        _engine = engine
        _session_factory = session_factory
    else:
        _role_engines[resolved] = engine
        _role_session_factories[resolved] = session_factory
    return engine


def get_session_factory(role: str | None = None) -> sessionmaker[Session]:
    """函数功能：`get_session_factory` 负责获取 session factory，服务于本文件职责：SQLAlchemy engine/session 生命周期。
    传参：
        role: role 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `sessionmaker[Session]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    resolved = _resolved_role(role)
    default_role = _resolved_role(None)
    if resolved == default_role:
        if _session_factory is None:
            get_engine()
        assert _session_factory is not None
        return _session_factory
    if resolved not in _role_session_factories:
        get_engine(resolved)
    return _role_session_factories[resolved]


@contextmanager
def session_scope(role: str | None = None) -> Iterator[Session]:
    """函数功能：`session_scope` 负责处理 session scope，服务于本文件职责：SQLAlchemy engine/session 生命周期。
    传参：
        role: role 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `Iterator[Session]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    session = get_session_factory(role)()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


def check_database_health() -> dict[str, str]:
    """函数功能：`check_database_health` 负责检查 database health，服务于本文件职责：SQLAlchemy engine/session 生命周期。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, str]`，表示结构化结果、载荷或状态映射。
    """
    with get_engine().connect() as conn:
        version = str(conn.execute(text("SELECT current_database() || ' / ' || version()" )).scalar_one())
    return {"status": "ok", "database": version}


def dispose_engine(role: str | None = None) -> None:
    """函数功能：`dispose_engine` 负责处理 dispose engine，服务于本文件职责：SQLAlchemy engine/session 生命周期。
    传参：
        role: role 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    global _engine, _session_factory
    if role is None:
        if _engine is not None:
            _engine.dispose()
        for engine in _role_engines.values():
            engine.dispose()
        _engine = None
        _session_factory = None
        _role_engines.clear()
        _role_session_factories.clear()
        return
    resolved = _resolved_role(role)
    default_role = _resolved_role(None)
    if resolved == default_role:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None
        return
    engine = _role_engines.pop(resolved, None)
    if engine is not None:
        engine.dispose()
    _role_session_factories.pop(resolved, None)
