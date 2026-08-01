"""文件作用：LLM 用量 Hook。

项目关系：本文件依赖 `agent.hooks.base`、`agent.hooks.context`、`core.config`、`core.settings` 等 7 个模块；被 `agent.hooks.manager`。
"""



from __future__ import annotations

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.config import get_chat_config
from core.settings import COORDINATION_BACKEND, LLM_CONCURRENCY_LIMIT, LLM_TOKEN_BUDGET_PER_MINUTE
from infrastructure.redis_keys import KEYS
from infrastructure.redis_rate_limit import LOCAL_RATE_LIMITER, RedisRateLimiter
from repositories.postgres.agent_runs import add_llm_usage


class LlmCapacityExceeded(RuntimeError):
    """类功能：`LlmCapacityExceeded` 封装与“LLM 用量 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `RuntimeError`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    pass


def _estimate_tokens(text: str) -> int:
    """函数功能：`_estimate_tokens` 负责估算 tokens，服务于本文件职责：LLM 用量 Hook。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    return max(1, len(text) // 4)


class LlmUsageHook(AgentHook):
    """类功能：`LlmUsageHook` 封装与“LLM 用量 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `AgentHook`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    name = "llm_usage"

    def before_llm(self, context: AgentRunContext, request: dict[str, object]) -> None:
        """函数功能：`LlmUsageHook.before_llm` 在类 `LlmUsageHook` 中负责处理 before llm，服务于本文件职责：LLM 用量 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            request: 请求对象或请求载荷，类型为 `dict[str, object]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
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
        """函数功能：`LlmUsageHook.after_llm` 在类 `LlmUsageHook` 中负责处理 after llm，服务于本文件职责：LLM 用量 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            request: 请求对象或请求载荷，类型为 `dict[str, object]`。
            result: 上游步骤返回的结果对象，类型为 `object`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
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
        """函数功能：`LlmUsageHook.on_error` 在类 `LlmUsageHook` 中负责处理 on error，服务于本文件职责：LLM 用量 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            error: 当前捕获的异常对象，类型为 `Exception`。
            scope: scope 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if scope in {"llm", "agent"}:
            self._release(context)

    @staticmethod
    def _release(context: AgentRunContext) -> None:
        """函数功能：`LlmUsageHook._release` 在类 `LlmUsageHook` 中负责释放，服务于本文件职责：LLM 用量 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        resources = context.resources.get("llm_slots") or []
        while resources:
            limiter, key, token = resources.pop()
            try:
                limiter.release_slot(key, token)
            except Exception:
                continue
