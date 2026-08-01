"""文件作用：Redis key 命名空间。

项目关系：本文件依赖 `core.settings`；被 `agent.hooks.idempotency`、`agent.hooks.llm_usage`、`agent.hooks.rate_limit`、`agent.hooks.space_lock` 等 20 个模块。
"""



from __future__ import annotations

import hashlib
from urllib.parse import quote

from core.settings import SUIXINJI_ENV


def _part(value: object) -> str:
    """函数功能：`_part` 负责处理 part，服务于本文件职责：Redis key 命名空间。
    传参：
        value: 待转换、校验或计算的值，类型为 `object`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return quote(str(value or "unknown"), safe="-_.")


def _hash(value: str) -> str:
    """函数功能：`_hash` 负责处理 hash，服务于本文件职责：Redis key 命名空间。
    传参：
        value: 待转换、校验或计算的值，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


class RedisKeys:
    """类功能：`RedisKeys` 封装与“Redis key 命名空间”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, env: str = SUIXINJI_ENV) -> None:
        """函数功能：`RedisKeys.__init__` 在类 `RedisKeys` 中负责初始化实例状态，服务于本文件职责：Redis key 命名空间。
        传参：
            env: env 参数，由调用方传入，类型为 `str`，默认值为 `SUIXINJI_ENV`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.prefix = f"sxj:{_part(env)}"

    def tenant(self, tenant_id: str) -> str:
        """函数功能：`RedisKeys.tenant` 在类 `RedisKeys` 中负责处理 tenant，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.prefix}:tenant:{_part(tenant_id)}"

    def rate_user(self, tenant_id: str, user_id: str, action: str) -> str:
        """函数功能：`RedisKeys.rate_user` 在类 `RedisKeys` 中负责计算比率 user，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str`。
            action: action 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:rate:user:{_part(user_id)}:{_part(action)}"

    def rate_tenant_tokens(self, tenant_id: str) -> str:
        """函数功能：`RedisKeys.rate_tenant_tokens` 在类 `RedisKeys` 中负责计算比率 tenant tokens，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:rate:llm-tokens"

    def concurrency_llm(self, tenant_id: str) -> str:
        """函数功能：`RedisKeys.concurrency_llm` 在类 `RedisKeys` 中负责处理 concurrency llm，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:concurrency:llm"

    def idempotency(self, tenant_id: str, source: str, message_id: str) -> str:
        """函数功能：`RedisKeys.idempotency` 在类 `RedisKeys` 中负责处理 idempotency，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            source: source 参数，由调用方传入，类型为 `str`。
            message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:idem:message:{_part(source)}:{_part(message_id)}"

    def lock_space(self, tenant_id: str, space_id: str) -> str:
        """函数功能：`RedisKeys.lock_space` 在类 `RedisKeys` 中负责加锁 space，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:lock:space:{_part(space_id)}"

    def lock_memory(self, tenant_id: str, memory_id: str) -> str:
        """函数功能：`RedisKeys.lock_memory` 在类 `RedisKeys` 中负责加锁 memory，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:lock:memory:{_part(memory_id)}"

    def lock_memory_key(self, tenant_id: str, space_id: str, memory_key: str) -> str:
        """函数功能：`RedisKeys.lock_memory_key` 在类 `RedisKeys` 中负责加锁 memory key，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            memory_key: memory key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:lock:memory-key:{_part(space_id)}:{_hash(memory_key)}"

    def lock_scheduler(self, job_name: str) -> str:
        """函数功能：`RedisKeys.lock_scheduler` 在类 `RedisKeys` 中负责加锁 scheduler，服务于本文件职责：Redis key 命名空间。
        传参：
            job_name: job name 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.prefix}:lock:scheduler:{_part(job_name)}"

    def cache_version(self, tenant_id: str, space_id: str) -> str:
        """函数功能：`RedisKeys.cache_version` 在类 `RedisKeys` 中负责缓存 version，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:cachever:space:{_part(space_id)}"

    def cache_search(self, tenant_id: str, kind: str, space_id: str, version: int, query_payload: str) -> str:
        """函数功能：`RedisKeys.cache_search` 在类 `RedisKeys` 中负责缓存 search，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            kind: kind 参数，由调用方传入，类型为 `str`。
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            version: version 参数，由调用方传入，类型为 `int`。
            query_payload: query payload 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:cache:{_part(kind)}:{_part(space_id)}:{version}:{_hash(query_payload)}"

    def cache_embedding(self, model: str, text: str) -> str:
        """函数功能：`RedisKeys.cache_embedding` 在类 `RedisKeys` 中负责缓存 embedding，服务于本文件职责：Redis key 命名空间。
        传参：
            model: model 参数，由调用方传入，类型为 `str`。
            text: 输入文本内容，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.prefix}:cache:embedding:{_part(model)}:{_hash(text)}"

    def memory_access_counts(self, tenant_id: str) -> str:
        """函数功能：`RedisKeys.memory_access_counts` 在类 `RedisKeys` 中负责处理 memory access counts，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:memory:access:counts"

    def memory_access_last_seen(self, tenant_id: str) -> str:
        """函数功能：`RedisKeys.memory_access_last_seen` 在类 `RedisKeys` 中负责处理 memory access last seen，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.tenant(tenant_id)}:memory:access:last-seen"

    def session(self, tenant_id: str, user_id: str) -> str:
        """函数功能：`RedisKeys.session` 在类 `RedisKeys` 中负责处理 session，服务于本文件职责：Redis key 命名空间。
        传参：
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
            user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.prefix}:session:{_part(tenant_id)}:{_part(user_id)}"

    def stream(self, task_type: str) -> str:
        """函数功能：`RedisKeys.stream` 在类 `RedisKeys` 中负责处理 stream，服务于本文件职责：Redis key 命名空间。
        传参：
            task_type: task type 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return f"{self.prefix}:stream:{_part(task_type)}"

    def dead_letter_stream(self) -> str:
        """函数功能：`RedisKeys.dead_letter_stream` 在类 `RedisKeys` 中负责处理 dead letter stream，服务于本文件职责：Redis key 命名空间。
        传参：
            无。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return self.stream("deadletter")


KEYS = RedisKeys()
