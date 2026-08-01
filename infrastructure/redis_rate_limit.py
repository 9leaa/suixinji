"""文件作用：Redis 限流器。

项目关系：本文件依赖 `infrastructure.redis_client`；被 `agent.hooks.llm_usage`、`agent.hooks.rate_limit`、`apps.api`、`tests.test_redis_coordination`。
"""



from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from redis import Redis

from infrastructure.redis_client import get_redis

_FIXED_WINDOW_LUA = """
local value = redis.call('INCRBY', KEYS[1], ARGV[1])
if value == tonumber(ARGV[1]) then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
local ttl = redis.call('PTTL', KEYS[1])
if value > tonumber(ARGV[3]) then
  return {0, value, ttl}
end
return {1, value, ttl}
"""

_ACQUIRE_SLOT_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
redis.call('PEXPIRE', KEYS[1], ARGV[5])
return 1
"""


@dataclass(frozen=True)
class LimitResult:
    """类功能：`LimitResult` 封装与“Redis 限流器”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    allowed: bool
    current: int
    retry_after_ms: int


class RedisRateLimiter:
    """类功能：`RedisRateLimiter` 封装与“Redis 限流器”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, client: Redis | None = None) -> None:
        """函数功能：`RedisRateLimiter.__init__` 在类 `RedisRateLimiter` 中负责初始化实例状态，服务于本文件职责：Redis 限流器。
        传参：
            client: 外部服务或基础设施客户端，类型为 `Redis | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client = client or get_redis()

    def allow(self, key: str, limit: int, window_seconds: int = 60, *, cost: int = 1) -> LimitResult:
        """函数功能：`RedisRateLimiter.allow` 在类 `RedisRateLimiter` 中负责处理 allow，服务于本文件职责：Redis 限流器。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
            limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`。
            window_seconds: window seconds 参数，由调用方传入，类型为 `int`，默认值为 `60`。
            cost: cost 参数，由调用方传入，类型为 `int`，默认值为 `1`。
        返回结果说明：
            返回 `LimitResult` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        allowed, current, ttl = self.client.eval(
            _FIXED_WINDOW_LUA,
            1,
            key,
            max(1, int(cost)),
            max(1, int(window_seconds * 1000)),
            max(1, int(limit)),
        )
        return LimitResult(bool(allowed), int(current), max(0, int(ttl)))

    def acquire_slot(self, key: str, limit: int, ttl_seconds: int = 60) -> str | None:
        """函数功能：`RedisRateLimiter.acquire_slot` 在类 `RedisRateLimiter` 中负责处理 acquire slot，服务于本文件职责：Redis 限流器。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
            limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`。
            ttl_seconds: ttl seconds 参数，由调用方传入，类型为 `int`，默认值为 `60`。
        返回结果说明：
            返回 `str | None`；未命中或无需处理时可返回 `None`。
        """
        now_ms = int(time.time() * 1000)
        token = uuid.uuid4().hex
        expires_ms = now_ms + max(1, int(ttl_seconds * 1000))
        acquired = self.client.eval(
            _ACQUIRE_SLOT_LUA,
            1,
            key,
            now_ms,
            max(1, int(limit)),
            expires_ms,
            token,
            max(1, int(ttl_seconds * 1000)),
        )
        return token if int(acquired) == 1 else None

    def release_slot(self, key: str, token: str) -> None:
        """函数功能：`RedisRateLimiter.release_slot` 在类 `RedisRateLimiter` 中负责释放 slot，服务于本文件职责：Redis 限流器。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
            token: token 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client.zrem(key, token)


class LocalRateLimiter:
    """类功能：`LocalRateLimiter` 封装与“Redis 限流器”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """

    def __init__(self) -> None:
        """函数功能：`LocalRateLimiter.__init__` 在类 `LocalRateLimiter` 中负责初始化实例状态，服务于本文件职责：Redis 限流器。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._lock = threading.RLock()
        self._windows: dict[str, tuple[float, int]] = {}
        self._slots: dict[str, dict[str, float]] = {}

    def allow(self, key: str, limit: int, window_seconds: int = 60, *, cost: int = 1) -> LimitResult:
        """函数功能：`LocalRateLimiter.allow` 在类 `LocalRateLimiter` 中负责处理 allow，服务于本文件职责：Redis 限流器。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
            limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`。
            window_seconds: window seconds 参数，由调用方传入，类型为 `int`，默认值为 `60`。
            cost: cost 参数，由调用方传入，类型为 `int`，默认值为 `1`。
        返回结果说明：
            返回 `LimitResult` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        now = time.monotonic()
        with self._lock:
            expires, current = self._windows.get(key, (now + window_seconds, 0))
            if expires <= now:
                expires, current = now + window_seconds, 0
            current += max(1, int(cost))
            self._windows[key] = (expires, current)
            return LimitResult(current <= limit, current, max(0, int((expires - now) * 1000)))

    def acquire_slot(self, key: str, limit: int, ttl_seconds: int = 60) -> str | None:
        """函数功能：`LocalRateLimiter.acquire_slot` 在类 `LocalRateLimiter` 中负责处理 acquire slot，服务于本文件职责：Redis 限流器。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
            limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`。
            ttl_seconds: ttl seconds 参数，由调用方传入，类型为 `int`，默认值为 `60`。
        返回结果说明：
            返回 `str | None`；未命中或无需处理时可返回 `None`。
        """
        now = time.monotonic()
        with self._lock:
            slots = self._slots.setdefault(key, {})
            for token, expires in list(slots.items()):
                if expires <= now:
                    slots.pop(token, None)
            if len(slots) >= limit:
                return None
            token = uuid.uuid4().hex
            slots[token] = now + ttl_seconds
            return token

    def release_slot(self, key: str, token: str) -> None:
        """函数功能：`LocalRateLimiter.release_slot` 在类 `LocalRateLimiter` 中负责释放 slot，服务于本文件职责：Redis 限流器。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
            token: token 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._lock:
            self._slots.get(key, {}).pop(token, None)


LOCAL_RATE_LIMITER = LocalRateLimiter()
