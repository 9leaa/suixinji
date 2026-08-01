"""文件作用：Redis 客户端工厂。

项目关系：本文件依赖 `core.settings`；被 `infrastructure.redis_cache`、`infrastructure.redis_idempotency`、`infrastructure.redis_lock`、`infrastructure.redis_rate_limit` 等 13 个模块。
"""



from __future__ import annotations

from redis import Redis
from redis.connection import ConnectionPool

from core.settings import (
    REDIS_BLOCKING_MAX_CONNECTIONS,
    REDIS_BLOCKING_SOCKET_TIMEOUT_SECONDS,
    REDIS_CONNECT_TIMEOUT_SECONDS,
    REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
    REDIS_MAX_CONNECTIONS,
    REDIS_SOCKET_TIMEOUT_SECONDS,
    REDIS_URL,
)

_pool: ConnectionPool | None = None
_client: Redis | None = None
_blocking_pool: ConnectionPool | None = None
_blocking_client: Redis | None = None


def get_redis() -> Redis:
    """函数功能：`get_redis` 负责获取 redis，服务于本文件职责：Redis 客户端工厂。
    传参：
        无。
    返回结果说明：
        返回 `Redis` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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


def get_blocking_redis() -> Redis:
    """函数功能：`get_blocking_redis` 负责获取 blocking redis，服务于本文件职责：Redis 客户端工厂。
    传参：
        无。
    返回结果说明：
        返回 `Redis` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    global _blocking_pool, _blocking_client
    if _blocking_client is not None:
        return _blocking_client
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL is not configured")
    _blocking_pool = ConnectionPool.from_url(
        REDIS_URL,
        max_connections=max(1, REDIS_BLOCKING_MAX_CONNECTIONS),
        socket_timeout=max(0.1, REDIS_BLOCKING_SOCKET_TIMEOUT_SECONDS),
        socket_connect_timeout=max(0.1, REDIS_CONNECT_TIMEOUT_SECONDS),
        health_check_interval=max(0, REDIS_HEALTH_CHECK_INTERVAL_SECONDS),
        decode_responses=True,
    )
    _blocking_client = Redis(connection_pool=_blocking_pool)
    return _blocking_client


def check_redis_health() -> dict[str, str]:
    """函数功能：`check_redis_health` 负责检查 redis health，服务于本文件职责：Redis 客户端工厂。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, str]`，表示结构化结果、载荷或状态映射。
    """
    client = get_redis()
    client.ping()
    info = client.info(section="server")
    return {"status": "ok", "redis_version": str(info.get("redis_version") or "unknown")}


def close_redis() -> None:
    """函数功能：`close_redis` 负责关闭 redis，服务于本文件职责：Redis 客户端工厂。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    global _pool, _client, _blocking_pool, _blocking_client
    if _client is not None:
        _client.close()
    if _blocking_client is not None:
        _blocking_client.close()
    if _pool is not None:
        _pool.disconnect()
    if _blocking_pool is not None:
        _blocking_pool.disconnect()
    _client = None
    _pool = None
    _blocking_client = None
    _blocking_pool = None
