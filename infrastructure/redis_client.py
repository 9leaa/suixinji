"""Shared synchronous Redis client with a small bounded connection pool."""

from __future__ import annotations

from redis import Redis
from redis.connection import ConnectionPool

from core.settings import (
    REDIS_CONNECT_TIMEOUT_SECONDS,
    REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
    REDIS_MAX_CONNECTIONS,
    REDIS_SOCKET_TIMEOUT_SECONDS,
    REDIS_URL,
)

_pool: ConnectionPool | None = None
_client: Redis | None = None


def get_redis() -> Redis:
    global _pool, _client
    if _client is not None:
        return _client
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL is not configured")
    _pool = ConnectionPool.from_url(
        REDIS_URL,
        max_connections=max(1, REDIS_MAX_CONNECTIONS),
        socket_timeout=max(0.1, REDIS_SOCKET_TIMEOUT_SECONDS),
        socket_connect_timeout=max(0.1, REDIS_CONNECT_TIMEOUT_SECONDS),
        health_check_interval=max(0, REDIS_HEALTH_CHECK_INTERVAL_SECONDS),
        decode_responses=True,
    )
    _client = Redis(connection_pool=_pool)
    return _client


def check_redis_health() -> dict[str, str]:
    client = get_redis()
    client.ping()
    info = client.info(section="server")
    return {"status": "ok", "redis_version": str(info.get("redis_version") or "unknown")}


def close_redis() -> None:
    global _pool, _client
    if _client is not None:
        _client.close()
    if _pool is not None:
        _pool.disconnect()
    _client = None
    _pool = None
