"""文件作用：Redis 分布式锁。

项目关系：本文件依赖 `core.observability`、`core.settings`、`infrastructure.database`、`infrastructure.redis_client`；被 `agent.hooks.space_lock`、`apps.handlers`、`apps.scheduler`、`memory.service` 等 5 个模块。
"""



from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from redis import Redis

from core.observability import log_event
from core.settings import COORDINATION_BACKEND, SPACE_LOCK_TTL_MS, SPACE_LOCK_WAIT_SECONDS
from infrastructure.database import get_engine
from infrastructure.redis_client import get_redis

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_GUARD = threading.Lock()


class RedisDistributedLock:
    """类功能：`RedisDistributedLock` 封装与“Redis 分布式锁”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, key: str, *, client: Redis | None = None, ttl_ms: int = SPACE_LOCK_TTL_MS) -> None:
        """函数功能：`RedisDistributedLock.__init__` 在类 `RedisDistributedLock` 中负责初始化实例状态，服务于本文件职责：Redis 分布式锁。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
            client: 外部服务或基础设施客户端，类型为 `Redis | None`，默认值为 `None`。
            ttl_ms: ttl ms 参数，由调用方传入，类型为 `int`，默认值为 `SPACE_LOCK_TTL_MS`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client = client or get_redis()
        self.key = key
        self.ttl_ms = max(100, int(ttl_ms))
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self, wait_seconds: float = SPACE_LOCK_WAIT_SECONDS) -> bool:
        """函数功能：`RedisDistributedLock.acquire` 在类 `RedisDistributedLock` 中负责处理 acquire，服务于本文件职责：Redis 分布式锁。
        传参：
            wait_seconds: wait seconds 参数，由调用方传入，类型为 `float`，默认值为 `SPACE_LOCK_WAIT_SECONDS`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        while True:
            if self.client.set(self.key, self.token, nx=True, px=self.ttl_ms):
                self.acquired = True
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def renew(self) -> bool:
        """函数功能：`RedisDistributedLock.renew` 在类 `RedisDistributedLock` 中负责处理 renew，服务于本文件职责：Redis 分布式锁。
        传参：
            无。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        return bool(self.client.eval(_RENEW_LUA, 1, self.key, self.token, self.ttl_ms))

    def release(self) -> bool:
        """函数功能：`RedisDistributedLock.release` 在类 `RedisDistributedLock` 中负责释放，服务于本文件职责：Redis 分布式锁。
        传参：
            无。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        if not self.acquired:
            return False
        released = bool(self.client.eval(_RELEASE_LUA, 1, self.key, self.token))
        self.acquired = False
        return released


@contextmanager
def postgres_advisory_lock(key: str) -> Iterator[None]:
    """函数功能：`postgres_advisory_lock` 负责加锁 postgres advisory，服务于本文件职责：Redis 分布式锁。
    传参：
        key: key 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `Iterator[None]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    connection = get_engine().connect()
    try:
        connection.exec_driver_sql("SELECT pg_advisory_lock(hashtext(%s))", (key,))
        yield
    finally:
        try:
            connection.exec_driver_sql("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        finally:
            connection.close()


@contextmanager
def coordinated_lock(key: str, *, critical: bool = True, wait_seconds: float = SPACE_LOCK_WAIT_SECONDS) -> Iterator[str]:
    """函数功能：`coordinated_lock` 负责加锁 coordinated，服务于本文件职责：Redis 分布式锁。
    传参：
        key: key 参数，由调用方传入，类型为 `str`。
        critical: critical 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        wait_seconds: wait seconds 参数，由调用方传入，类型为 `float`，默认值为 `SPACE_LOCK_WAIT_SECONDS`。
    返回结果说明：
        返回 `Iterator[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    started = time.monotonic()

    def record(backend: str) -> None:
        """函数功能：`record` 负责记录，服务于本文件职责：Redis 分布式锁。
        传参：
            backend: backend 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        wait_ms = int((time.monotonic() - started) * 1000)
        log_event(
            "runtime.lock_acquired",
            status="completed",
            duration_ms=wait_ms,
            extra={"backend": backend, "lock_wait_ms": wait_ms},
        )

    if COORDINATION_BACKEND == "redis":
        lock: RedisDistributedLock | None = None
        acquired = False
        try:
            lock = RedisDistributedLock(key)
            acquired = lock.acquire(wait_seconds)
        except Exception:
            if not critical:
                raise
        if acquired:
            assert lock is not None
            record("redis")
            stop_renewal = threading.Event()

            def renew_loop() -> None:
                """函数功能：`renew_loop` 负责处理 renew loop，服务于本文件职责：Redis 分布式锁。
                传参：
                    无。
                返回结果说明：
                    无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
                """
                interval = max(0.1, lock.ttl_ms / 3000)
                while not stop_renewal.wait(interval):
                    try:
                        if not lock.renew():
                            return
                    except Exception:
                        return

            renewal = threading.Thread(target=renew_loop, name="redis-lock-renewal", daemon=True)
            renewal.start()
            try:
                yield "redis"
            finally:
                stop_renewal.set()
                try:
                    lock.release()
                except Exception as exc:
                    log_event(
                        "runtime.lock_release_failed",
                        level="warning",
                        status="degraded",
                        error=type(exc).__name__,
                        extra={"backend": "redis", "key": key},
                    )
            return
        if not critical:
            raise TimeoutError(f"could not acquire Redis lock: {key}")
        with postgres_advisory_lock(key):
            record("postgres")
            yield "postgres"
        return

    with _LOCAL_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(key, threading.RLock())
    with local_lock:
        record("local")
        yield "local"
