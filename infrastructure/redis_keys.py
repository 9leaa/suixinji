"""Central Redis key builder; every key is namespaced by environment."""

from __future__ import annotations

import hashlib
from urllib.parse import quote

from core.settings import SUIXINJI_ENV


def _part(value: object) -> str:
    """负责“part”。

    该函数是 `infrastructure.redis_keys` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return quote(str(value or "unknown"), safe="-_.")


def _hash(value: str) -> str:
    """负责“hash”。

    该函数是 `infrastructure.redis_keys` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


class RedisKeys:
    def __init__(self, env: str = SUIXINJI_ENV) -> None:
        """初始化`RedisKeys` 实例并建立后续调用所需的状态。"""
        self.prefix = f"sxj:{_part(env)}"

    def tenant(self, tenant_id: str) -> str:
        """负责“租户”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.prefix}:tenant:{_part(tenant_id)}"

    def rate_user(self, tenant_id: str, user_id: str, action: str) -> str:
        """负责“rate用户”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:rate:user:{_part(user_id)}:{_part(action)}"

    def rate_tenant_tokens(self, tenant_id: str) -> str:
        """负责“rate租户tokens”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:rate:llm-tokens"

    def concurrency_llm(self, tenant_id: str) -> str:
        """负责“concurrencyLLM”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:concurrency:llm"

    def idempotency(self, tenant_id: str, source: str, message_id: str) -> str:
        """负责“idempotency”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:idem:message:{_part(source)}:{_part(message_id)}"

    def lock_space(self, tenant_id: str, space_id: str) -> str:
        """负责“锁空间”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:lock:space:{_part(space_id)}"

    def lock_memory(self, tenant_id: str, memory_id: str) -> str:
        """负责“锁记忆”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:lock:memory:{_part(memory_id)}"

    def lock_memory_key(self, tenant_id: str, space_id: str, memory_key: str) -> str:
        """负责“锁记忆键”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:lock:memory-key:{_part(space_id)}:{_hash(memory_key)}"

    def lock_scheduler(self, job_name: str) -> str:
        """负责“锁scheduler”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.prefix}:lock:scheduler:{_part(job_name)}"

    def cache_version(self, tenant_id: str, space_id: str) -> str:
        """负责“缓存版本”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:cachever:space:{_part(space_id)}"

    def cache_search(self, tenant_id: str, kind: str, space_id: str, version: int, query_payload: str) -> str:
        """负责“缓存检索”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:cache:{_part(kind)}:{_part(space_id)}:{version}:{_hash(query_payload)}"

    def cache_embedding(self, model: str, text: str) -> str:
        """负责“缓存向量”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.prefix}:cache:embedding:{_part(model)}:{_hash(text)}"

    def memory_access_counts(self, tenant_id: str) -> str:
        """负责“记忆accesscounts”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:memory:access:counts"

    def memory_access_last_seen(self, tenant_id: str) -> str:
        """负责“记忆accesslastseen”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.tenant(tenant_id)}:memory:access:last-seen"

    def session(self, tenant_id: str, user_id: str) -> str:
        """负责“会话”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.prefix}:session:{_part(tenant_id)}:{_part(user_id)}"

    def stream(self, task_type: str) -> str:
        """负责“流”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return f"{self.prefix}:stream:{_part(task_type)}"

    def dead_letter_stream(self) -> str:
        """负责“deadletter流”。

        该函数是 `infrastructure.redis_keys` 中的`RedisKeys` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return self.stream("deadletter")


KEYS = RedisKeys()
