"""LLM concurrency, token budget, and usage persistence hook."""

from __future__ import annotations

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.config import get_chat_config
from core.settings import COORDINATION_BACKEND, LLM_CONCURRENCY_LIMIT, LLM_TOKEN_BUDGET_PER_MINUTE
from infrastructure.redis_keys import KEYS
from infrastructure.redis_rate_limit import LOCAL_RATE_LIMITER, RedisRateLimiter
from repositories.postgres.agent_runs import add_llm_usage


class LlmCapacityExceeded(RuntimeError):
    pass


def _estimate_tokens(text: str) -> int:
    """负责“estimatetokens”。

    该函数是 `agent.hooks.llm_usage` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return max(1, len(text) // 4)


class LlmUsageHook(AgentHook):
    name = "llm_usage"

    def before_llm(self, context: AgentRunContext, request: dict[str, object]) -> None:
        """负责“LLM 调用前的 Hook 前置处理”。

        该函数是 `agent.hooks.llm_usage` 中的`LlmUsageHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        limiter = LOCAL_RATE_LIMITER
        if COORDINATION_BACKEND == "redis":
            try:
                limiter = RedisRateLimiter()
                budget = limiter.allow(
                    KEYS.rate_tenant_tokens(context.tenant_id),
                    LLM_TOKEN_BUDGET_PER_MINUTE,
                    cost=_estimate_tokens(str(request.get("user_prompt") or "")),
                )
            except Exception:
                limiter = LOCAL_RATE_LIMITER
                budget = limiter.allow(
                    KEYS.rate_tenant_tokens(context.tenant_id),
                    max(1, LLM_TOKEN_BUDGET_PER_MINUTE // 2),
                    cost=_estimate_tokens(str(request.get("user_prompt") or "")),
                )
        else:
            budget = limiter.allow(
                KEYS.rate_tenant_tokens(context.tenant_id),
                LLM_TOKEN_BUDGET_PER_MINUTE,
                cost=_estimate_tokens(str(request.get("user_prompt") or "")),
            )
        if not budget.allowed:
            raise LlmCapacityExceeded("tenant LLM token budget exceeded")
        slot_key = KEYS.concurrency_llm(context.tenant_id)
        token = limiter.acquire_slot(slot_key, LLM_CONCURRENCY_LIMIT)
        if token is None:
            raise LlmCapacityExceeded("tenant LLM concurrency limit exceeded")
        context.resources.setdefault("llm_slots", []).append((limiter, slot_key, token))
        request["estimated_input_tokens"] = _estimate_tokens(str(request.get("user_prompt") or ""))

    def after_llm(self, context: AgentRunContext, request: dict[str, object], result: object) -> None:
        """负责“LLM 调用后的 Hook 后置处理”。

        该函数是 `agent.hooks.llm_usage` 中的`LlmUsageHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        self._release(context)
        input_tokens = int(request.get("estimated_input_tokens") or 0)
        output_tokens = _estimate_tokens(str(result))
        try:
            add_llm_usage(
                context.run_id,
                model=get_chat_config().model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception:
            return

    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
        """负责“异常发生时的 Hook 错误处理”。

        该函数是 `agent.hooks.llm_usage` 中的`LlmUsageHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        if scope in {"llm", "agent"}:
            self._release(context)

    @staticmethod
    def _release(context: AgentRunContext) -> None:
        """负责“释放”。

        该函数是 `agent.hooks.llm_usage` 中的`LlmUsageHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        resources = context.resources.get("llm_slots") or []
        while resources:
            limiter, key, token = resources.pop()
            try:
                limiter.release_slot(key, token)
            except Exception:
                continue
