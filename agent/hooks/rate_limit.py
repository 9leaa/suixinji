"""Per-user Agent request limiting with conservative local fallback."""

from __future__ import annotations

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.settings import COORDINATION_BACKEND, RATE_LIMIT_ASK_PER_MINUTE, RATE_LIMIT_INGEST_PER_MINUTE
from infrastructure.redis_keys import KEYS
from infrastructure.redis_rate_limit import LOCAL_RATE_LIMITER, RedisRateLimiter


class RateLimitExceeded(RuntimeError):
    pass


class RateLimitHook(AgentHook):
    name = "rate_limit"

    def before_agent(self, context: AgentRunContext) -> None:
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
