"""文件作用：限流 Hook。

项目关系：本文件依赖 `agent.hooks.base`、`agent.hooks.context`、`core.settings`、`infrastructure.redis_keys` 等 5 个模块；被 `agent.hooks.manager`。
"""



from __future__ import annotations

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.settings import COORDINATION_BACKEND, RATE_LIMIT_ASK_PER_MINUTE, RATE_LIMIT_INGEST_PER_MINUTE
from infrastructure.redis_keys import KEYS
from infrastructure.redis_rate_limit import LOCAL_RATE_LIMITER, RedisRateLimiter


class RateLimitExceeded(RuntimeError):
    """类功能：`RateLimitExceeded` 封装与“限流 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `RuntimeError`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    pass


class RateLimitHook(AgentHook):
    """类功能：`RateLimitHook` 封装与“限流 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `AgentHook`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    name = "rate_limit"

    def before_agent(self, context: AgentRunContext) -> None:
        """函数功能：`RateLimitHook.before_agent` 在类 `RateLimitHook` 中负责处理 before agent，服务于本文件职责：限流 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        action = "ingest" if context.run_type in {"ingest", "memory"} else "ask"
        limit = RATE_LIMIT_INGEST_PER_MINUTE if action == "ingest" else RATE_LIMIT_ASK_PER_MINUTE
        key = KEYS.rate_user(context.tenant_id, context.user_id, action)
        limiter = LOCAL_RATE_LIMITER
        if COORDINATION_BACKEND == "redis":
            try:
                limiter = RedisRateLimiter()
                result = limiter.allow(key, limit)
            except Exception:
                result = LOCAL_RATE_LIMITER.allow(key, max(1, limit // 2))
        else:
            result = limiter.allow(key, limit)
        context.metadata["rate_limit"] = {"current": result.current, "retry_after_ms": result.retry_after_ms}
        if not result.allowed:
            raise RateLimitExceeded(f"request rate limit exceeded; retry after {result.retry_after_ms}ms")
