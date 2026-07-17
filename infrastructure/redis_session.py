"""Short-lived Redis conversation session state."""

from __future__ import annotations

import json
from typing import Any

from redis import Redis

from core.settings import SESSION_TTL_SECONDS
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import KEYS, RedisKeys


class RedisSessionStore:
    def __init__(self, client: Redis | None = None, ttl_seconds: int = SESSION_TTL_SECONDS, keys: RedisKeys = KEYS) -> None:
        self.client = client or get_redis()
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.keys = keys

    def get(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        raw = self.client.get(self.keys.session(tenant_id, user_id))
        return dict(json.loads(raw)) if raw else {}

    def set(self, tenant_id: str, user_id: str, session: dict[str, Any]) -> None:
        self.client.set(
            self.keys.session(tenant_id, user_id),
            json.dumps(session, ensure_ascii=False, default=str),
            ex=self.ttl_seconds,
        )

    def delete(self, tenant_id: str, user_id: str) -> None:
        self.client.delete(self.keys.session(tenant_id, user_id))

    def touch(self, tenant_id: str, user_id: str) -> None:
        self.client.expire(self.keys.session(tenant_id, user_id), self.ttl_seconds)
