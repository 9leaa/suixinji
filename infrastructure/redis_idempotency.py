"""Fast Redis idempotency state with a process-local fallback."""

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
    def __init__(self, client: Redis | None = None, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS) -> None:
        self.client = client or get_redis()
        self.ttl_seconds = max(1, int(ttl_seconds))

    def begin(self, key: str) -> bool:
        return bool(self.client.eval(_BEGIN_LUA, 1, key, self.ttl_seconds))

    def complete(self, key: str) -> None:
        self.client.set(key, "completed", ex=self.ttl_seconds)

    def fail(self, key: str) -> None:
        self.client.set(key, "failed", ex=min(self.ttl_seconds, 60))

    def get(self, key: str) -> str | None:
        value = self.client.get(key)
        return str(value) if value is not None else None


class LocalIdempotencyStore:
    def __init__(self, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._lock = threading.RLock()
        self._items: dict[str, tuple[str, float]] = {}

    def begin(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            old = self._items.get(key)
            if old is not None and old[1] > now:
                return False
            self._items[key] = ("processing", now + self.ttl_seconds)
            return True

    def complete(self, key: str) -> None:
        with self._lock:
            self._items[key] = ("completed", time.monotonic() + self.ttl_seconds)

    def fail(self, key: str) -> None:
        with self._lock:
            self._items[key] = ("failed", time.monotonic() + min(self.ttl_seconds, 60))

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._items.get(key)
            if item is None or item[1] <= time.monotonic():
                self._items.pop(key, None)
                return None
            return item[0]


LOCAL_IDEMPOTENCY = LocalIdempotencyStore()
