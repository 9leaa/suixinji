"""Versioned Redis cache for read-only Agent tools."""

from __future__ import annotations

import json
from typing import Any

from redis import Redis

from core.settings import CACHE_ENABLED, CACHE_SEARCH_TTL_SECONDS, COORDINATION_BACKEND
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import KEYS, RedisKeys


class RedisCache:
    def __init__(self, client: Redis | None = None, *, enabled: bool = CACHE_ENABLED, keys: RedisKeys = KEYS) -> None:
        self.client = client or get_redis()
        self.enabled = enabled
        self.keys = keys

    def version(self, space_id: str) -> int:
        if not self.enabled:
            return 0
        value = self.client.get(self.keys.cache_version(space_id))
        return int(value or 0)

    def bump_version(self, space_id: str) -> int:
        if not self.enabled:
            return 0
        return int(self.client.incr(self.keys.cache_version(space_id)))

    def get(self, kind: str, space_id: str, query_payload: str) -> Any | None:
        if not self.enabled:
            return None
        key = self.keys.cache_search(kind, space_id, self.version(space_id), query_payload)
        raw = self.client.get(key)
        return json.loads(raw) if raw else None

    def set(self, kind: str, space_id: str, query_payload: str, value: Any, ttl_seconds: int = CACHE_SEARCH_TTL_SECONDS) -> None:
        if not self.enabled:
            return
        key = self.keys.cache_search(kind, space_id, self.version(space_id), query_payload)
        self.client.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=max(1, int(ttl_seconds)))


def invalidate_space_cache(space_id: str) -> None:
    if COORDINATION_BACKEND != "redis" or not CACHE_ENABLED:
        return
    try:
        RedisCache().bump_version(space_id)
    except Exception:
        return
