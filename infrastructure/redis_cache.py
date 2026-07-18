"""Versioned Redis cache for read-only Agent tools."""

from __future__ import annotations

import json
from typing import Any

from redis import Redis

from core.settings import CACHE_ENABLED, CACHE_SEARCH_TTL_SECONDS, COORDINATION_BACKEND, EMBEDDING_CACHE_TTL_SECONDS
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


class EmbeddingCache:
    def __init__(self, client: Redis | None = None, *, enabled: bool = CACHE_ENABLED, keys: RedisKeys = KEYS) -> None:
        self.client = client or get_redis()
        self.enabled = enabled
        self.keys = keys

    def get(self, model: str, text: str) -> list[float] | None:
        if not self.enabled:
            return None
        raw = self.client.get(self.keys.cache_embedding(model, text))
        if not raw:
            return None
        try:
            value = json.loads(raw)
            return [float(item) for item in value] if isinstance(value, list) and value else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def set(self, model: str, text: str, embedding: list[float], *, ttl_seconds: int = EMBEDDING_CACHE_TTL_SECONDS) -> None:
        if not self.enabled:
            return
        self.client.set(
            self.keys.cache_embedding(model, text),
            json.dumps(embedding, separators=(",", ":")),
            ex=max(1, int(ttl_seconds)),
        )


class MemoryAccessBuffer:
    """Approximate access counters; Memory correctness never depends on them."""

    _DRAIN_SCRIPT = """
local scan = redis.call('HSCAN', KEYS[1], '0', 'COUNT', ARGV[1])
local items = scan[2]
local result = {}
for index = 1, #items, 2 do
  local memory_id = items[index]
  local count = items[index + 1]
  local last_seen = redis.call('HGET', KEYS[2], memory_id) or ''
  redis.call('HDEL', KEYS[1], memory_id)
  redis.call('HDEL', KEYS[2], memory_id)
  table.insert(result, memory_id)
  table.insert(result, count)
  table.insert(result, last_seen)
end
return result
"""

    def __init__(self, client: Redis | None = None, *, keys: RedisKeys = KEYS) -> None:
        self.client = client or get_redis()
        self.keys = keys

    def increment(self, memory_ids: list[str], *, seen_at: str) -> None:
        unique_ids = list(dict.fromkeys(str(memory_id) for memory_id in memory_ids if memory_id))
        if not unique_ids:
            return
        pipeline = self.client.pipeline(transaction=False)
        for memory_id in unique_ids:
            pipeline.hincrby(self.keys.memory_access_counts(), memory_id, 1)
            pipeline.hset(self.keys.memory_access_last_seen(), memory_id, seen_at)
        pipeline.execute()

    def drain(self, *, limit: int) -> dict[str, tuple[int, str]]:
        raw = self.client.eval(
            self._DRAIN_SCRIPT,
            2,
            self.keys.memory_access_counts(),
            self.keys.memory_access_last_seen(),
            max(1, int(limit)),
        )
        result: dict[str, tuple[int, str]] = {}
        for index in range(0, len(raw), 3):
            result[str(raw[index])] = (int(raw[index + 1]), str(raw[index + 2]))
        return result

    def restore(self, entries: dict[str, tuple[int, str]]) -> None:
        if not entries:
            return
        pipeline = self.client.pipeline(transaction=False)
        for memory_id, (count, last_seen) in entries.items():
            pipeline.hincrby(self.keys.memory_access_counts(), memory_id, count)
            if last_seen:
                pipeline.hset(self.keys.memory_access_last_seen(), memory_id, last_seen)
        pipeline.execute()


def invalidate_space_cache(space_id: str) -> None:
    if COORDINATION_BACKEND != "redis" or not CACHE_ENABLED:
        return
    try:
        RedisCache().bump_version(space_id)
    except Exception:
        return
