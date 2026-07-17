"""PostgreSQL engine, transaction, and health-check helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from core.settings import (
    DATABASE_MAX_OVERFLOW,
    DATABASE_POOL_RECYCLE_SECONDS,
    DATABASE_POOL_SIZE,
    DATABASE_POOL_TIMEOUT_SECONDS,
    DATABASE_URL,
)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _normalized_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is not None:
        return _engine
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    _engine = create_engine(
        _normalized_database_url(DATABASE_URL),
        pool_pre_ping=True,
        pool_size=max(1, DATABASE_POOL_SIZE),
        max_overflow=max(0, DATABASE_MAX_OVERFLOW),
        pool_timeout=max(1, DATABASE_POOL_TIMEOUT_SECONDS),
        pool_recycle=max(60, DATABASE_POOL_RECYCLE_SECONDS),
        future=True,
    )
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


def check_database_health() -> dict[str, str]:
    with get_engine().connect() as conn:
        version = str(conn.execute(text("SELECT current_database() || ' / ' || version()" )).scalar_one())
    return {"status": "ok", "database": version}


def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
