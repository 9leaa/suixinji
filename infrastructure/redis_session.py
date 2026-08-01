"""文件作用：Redis 会话存储。

项目关系：本文件依赖 `core.settings`、`infrastructure.redis_client`、`infrastructure.redis_keys`；被 `agent.hooks.session`、`tests.test_redis_coordination`。
"""



from __future__ import annotations

import json
from typing import Any

from redis import Redis

from core.settings import SESSION_TTL_SECONDS
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import KEYS, RedisKeys


class RedisSessionStore:
    """类功能：`RedisSessionStore` 封装与“Redis 会话存储”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, client: Redis | None = None, ttl_seconds: int = SESSION_TTL_SECONDS, keys: RedisKeys = KEYS) -> None:
        """函数功能：`RedisSessionStore.__init__` 在类 `RedisSessionStore` 中负责初始化实例状态，服务于本文件职责：Redis 会话存储。
        传参：
            client: 外部服务或基础设施客户端，类型为 `Redis | None`，默认值为 `None`。
            ttl_seconds: ttl seconds 参数，由调用方传入，类型为 `int`，默认值为 `SESSION_TTL_SECONDS`。
            keys: keys 参数，由调用方传入，类型为 `RedisKeys`，默认值为 `KEYS`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client = client or get_redis()
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.keys = keys

    def get(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        """函数功能：`RedisSessionStore.get` 在类 `RedisSessionStore` 中负责获取，服务于本文件职责：Redis 会话存储。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str`。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        raw = self.client.get(self.keys.session(tenant_id, user_id))
        return dict(json.loads(raw)) if raw else {}

    def set(self, tenant_id: str, user_id: str, session: dict[str, Any]) -> None:
        """函数功能：`RedisSessionStore.set` 在类 `RedisSessionStore` 中负责设置，服务于本文件职责：Redis 会话存储。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str`。
            session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `dict[str, Any]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client.set(
            self.keys.session(tenant_id, user_id),
            json.dumps(session, ensure_ascii=False, default=str),
            ex=self.ttl_seconds,
        )

    def delete(self, tenant_id: str, user_id: str) -> None:
        """函数功能：`RedisSessionStore.delete` 在类 `RedisSessionStore` 中负责删除，服务于本文件职责：Redis 会话存储。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client.delete(self.keys.session(tenant_id, user_id))

    def touch(self, tenant_id: str, user_id: str) -> None:
        """函数功能：`RedisSessionStore.touch` 在类 `RedisSessionStore` 中负责处理 touch，服务于本文件职责：Redis 会话存储。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client.expire(self.keys.session(tenant_id, user_id), self.ttl_seconds)
