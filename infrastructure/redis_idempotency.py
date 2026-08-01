"""文件作用：Redis 幂等锁/结果。

项目关系：本文件依赖 `core.settings`、`infrastructure.redis_client`；被 `agent.hooks.idempotency`、`apps.receiver`、`tests.test_redis_coordination`。
"""



from __future__ import annotations

import threading
import time

from redis import Redis

from core.settings import IDEMPOTENCY_TTL_SECONDS
from infrastructure.redis_client import get_redis

_BEGIN_LUA = """
local current = redis.call('GET', KEYS[1])
if not current or current == 'failed' then
  redis.call('SET', KEYS[1], 'processing', 'EX', ARGV[1])
  return 1
end
return 0
"""


class IdempotencyStore:
    """类功能：`IdempotencyStore` 封装与“Redis 幂等锁/结果”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, client: Redis | None = None, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS) -> None:
        """函数功能：`IdempotencyStore.__init__` 在类 `IdempotencyStore` 中负责初始化实例状态，服务于本文件职责：Redis 幂等锁/结果。
        传参：
            client: 外部服务或基础设施客户端，类型为 `Redis | None`，默认值为 `None`。
            ttl_seconds: ttl seconds 参数，由调用方传入，类型为 `int`，默认值为 `IDEMPOTENCY_TTL_SECONDS`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client = client or get_redis()
        self.ttl_seconds = max(1, int(ttl_seconds))

    def begin(self, key: str) -> bool:
        """函数功能：`IdempotencyStore.begin` 在类 `IdempotencyStore` 中负责处理 begin，服务于本文件职责：Redis 幂等锁/结果。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        return bool(self.client.eval(_BEGIN_LUA, 1, key, self.ttl_seconds))

    def complete(self, key: str) -> None:
        """函数功能：`IdempotencyStore.complete` 在类 `IdempotencyStore` 中负责完成，服务于本文件职责：Redis 幂等锁/结果。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client.set(key, "completed", ex=self.ttl_seconds)

    def fail(self, key: str) -> None:
        """函数功能：`IdempotencyStore.fail` 在类 `IdempotencyStore` 中负责处理 fail，服务于本文件职责：Redis 幂等锁/结果。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client.set(key, "failed", ex=min(self.ttl_seconds, 60))

    def get(self, key: str) -> str | None:
        """函数功能：`IdempotencyStore.get` 在类 `IdempotencyStore` 中负责获取，服务于本文件职责：Redis 幂等锁/结果。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `str | None`；未命中或无需处理时可返回 `None`。
        """
        value = self.client.get(key)
        return str(value) if value is not None else None


class LocalIdempotencyStore:
    """类功能：`LocalIdempotencyStore` 封装与“Redis 幂等锁/结果”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS) -> None:
        """函数功能：`LocalIdempotencyStore.__init__` 在类 `LocalIdempotencyStore` 中负责初始化实例状态，服务于本文件职责：Redis 幂等锁/结果。
        传参：
            ttl_seconds: ttl seconds 参数，由调用方传入，类型为 `int`，默认值为 `IDEMPOTENCY_TTL_SECONDS`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._lock = threading.RLock()
        self._items: dict[str, tuple[str, float]] = {}

    def begin(self, key: str) -> bool:
        """函数功能：`LocalIdempotencyStore.begin` 在类 `LocalIdempotencyStore` 中负责处理 begin，服务于本文件职责：Redis 幂等锁/结果。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        now = time.monotonic()
        with self._lock:
            old = self._items.get(key)
            if old is not None and old[1] > now:
                return False
            self._items[key] = ("processing", now + self.ttl_seconds)
            return True

    def complete(self, key: str) -> None:
        """函数功能：`LocalIdempotencyStore.complete` 在类 `LocalIdempotencyStore` 中负责完成，服务于本文件职责：Redis 幂等锁/结果。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._lock:
            self._items[key] = ("completed", time.monotonic() + self.ttl_seconds)

    def fail(self, key: str) -> None:
        """函数功能：`LocalIdempotencyStore.fail` 在类 `LocalIdempotencyStore` 中负责处理 fail，服务于本文件职责：Redis 幂等锁/结果。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._lock:
            self._items[key] = ("failed", time.monotonic() + min(self.ttl_seconds, 60))

    def get(self, key: str) -> str | None:
        """函数功能：`LocalIdempotencyStore.get` 在类 `LocalIdempotencyStore` 中负责获取，服务于本文件职责：Redis 幂等锁/结果。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `str | None`；未命中或无需处理时可返回 `None`。
        """
        with self._lock:
            item = self._items.get(key)
            if item is None or item[1] <= time.monotonic():
                self._items.pop(key, None)
                return None
            return item[0]


LOCAL_IDEMPOTENCY = LocalIdempotencyStore()
