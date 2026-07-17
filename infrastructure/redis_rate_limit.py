"""Redis-backed fixed-window limits and expiring concurrency slots."""

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
    allowed: bool
    current: int
    retry_after_ms: int


class RedisRateLimiter:
    def __init__(self, client: Redis | None = None) -> None:
        self.client = client or get_redis()

    def allow(self, key: str, limit: int, window_seconds: int = 60, *, cost: int = 1) -> LimitResult:
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
        self.client.zrem(key, token)


class LocalRateLimiter:
    """Conservative process-local fallback used when Redis is unavailable."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._windows: dict[str, tuple[float, int]] = {}
        self._slots: dict[str, dict[str, float]] = {}

    def allow(self, key: str, limit: int, window_seconds: int = 60, *, cost: int = 1) -> LimitResult:
        now = time.monotonic()
        with self._lock:
            expires, current = self._windows.get(key, (now + window_seconds, 0))
            if expires <= now:
                expires, current = now + window_seconds, 0
            current += max(1, int(cost))
            self._windows[key] = (expires, current)
            return LimitResult(current <= limit, current, max(0, int((expires - now) * 1000)))

    def acquire_slot(self, key: str, limit: int, ttl_seconds: int = 60) -> str | None:
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
        with self._lock:
            self._slots.get(key, {}).pop(token, None)


LOCAL_RATE_LIMITER = LocalRateLimiter()
