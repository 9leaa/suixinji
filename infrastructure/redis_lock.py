"""Token-safe Redis locks with PostgreSQL/local fallbacks."""

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
    def __init__(self, key: str, *, client: Redis | None = None, ttl_ms: int = SPACE_LOCK_TTL_MS) -> None:
        self.client = client or get_redis()
        self.key = key
        self.ttl_ms = max(100, int(ttl_ms))
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self, wait_seconds: float = SPACE_LOCK_WAIT_SECONDS) -> bool:
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        while True:
            if self.client.set(self.key, self.token, nx=True, px=self.ttl_ms):
                self.acquired = True
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def renew(self) -> bool:
        return bool(self.client.eval(_RENEW_LUA, 1, self.key, self.token, self.ttl_ms))

    def release(self) -> bool:
        if not self.acquired:
            return False
        released = bool(self.client.eval(_RELEASE_LUA, 1, self.key, self.token))
        self.acquired = False
        return released


@contextmanager
def postgres_advisory_lock(key: str) -> Iterator[None]:
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
    started = time.monotonic()

    def record(backend: str) -> None:
        wait_ms = int((time.monotonic() - started) * 1000)
        log_event(
            "runtime.lock_acquired",
            status="completed",
            duration_ms=wait_ms,
            extra={"backend": backend, "lock_wait_ms": wait_ms},
        )

    if COORDINATION_BACKEND == "redis":
        lock = RedisDistributedLock(key)
        try:
            if lock.acquire(wait_seconds):
                record("redis")
                stop_renewal = threading.Event()

                def renew_loop() -> None:
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
                    lock.release()
                return
            if not critical:
                raise TimeoutError(f"could not acquire Redis lock: {key}")
        except Exception:
            if not critical:
                raise
        with postgres_advisory_lock(key):
            record("postgres")
            yield "postgres"
        return

    with _LOCAL_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(key, threading.RLock())
    with local_lock:
        record("local")
        yield "local"
