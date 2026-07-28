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
        """初始化`RedisSessionStore` 实例并建立后续调用所需的状态。"""
        self.client = client or get_redis()
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.keys = keys

    def get(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        """负责“获取”。

        该函数是 `infrastructure.redis_session` 中的`RedisSessionStore` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        raw = self.client.get(self.keys.session(tenant_id, user_id))
        return dict(json.loads(raw)) if raw else {}

    def set(self, tenant_id: str, user_id: str, session: dict[str, Any]) -> None:
        """负责“设置”。

        该函数是 `infrastructure.redis_session` 中的`RedisSessionStore` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        self.client.set(
            self.keys.session(tenant_id, user_id),
            json.dumps(session, ensure_ascii=False, default=str),
            ex=self.ttl_seconds,
        )

    def delete(self, tenant_id: str, user_id: str) -> None:
        """负责“删除”。

        该函数是 `infrastructure.redis_session` 中的`RedisSessionStore` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        self.client.delete(self.keys.session(tenant_id, user_id))

    def touch(self, tenant_id: str, user_id: str) -> None:
        """负责“touch”。

        该函数是 `infrastructure.redis_session` 中的`RedisSessionStore` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        self.client.expire(self.keys.session(tenant_id, user_id), self.ttl_seconds)
