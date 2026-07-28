"""Agent request idempotency backed by Redis and PostgreSQL final constraints."""

from __future__ import annotations

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.settings import COORDINATION_BACKEND
from infrastructure.redis_idempotency import IdempotencyStore, LOCAL_IDEMPOTENCY
from infrastructure.redis_keys import KEYS


class DuplicateAgentRequest(RuntimeError):
    pass


class IdempotencyHook(AgentHook):
    name = "idempotency"

    def before_agent(self, context: AgentRunContext) -> None:
        """负责“Agent 执行前的 Hook 前置处理”。

        该函数是 `agent.hooks.idempotency` 中的`IdempotencyHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        if not context.message_id or context.task_id:
            return
        key = KEYS.idempotency(context.tenant_id, context.run_type, context.message_id)
        store = LOCAL_IDEMPOTENCY
        if COORDINATION_BACKEND == "redis":
            try:
                store = IdempotencyStore()
                acquired = store.begin(key)
            except Exception:
                acquired = LOCAL_IDEMPOTENCY.begin(key)
                store = LOCAL_IDEMPOTENCY
        else:
            acquired = store.begin(key)
        if not acquired:
            raise DuplicateAgentRequest(f"duplicate {context.run_type} request: {context.message_id}")
        context.resources["idempotency"] = (store, key)

    def after_agent(self, context: AgentRunContext, result: object) -> None:
        """负责“Agent 执行后的 Hook 后置处理”。

        该函数是 `agent.hooks.idempotency` 中的`IdempotencyHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        resource = context.resources.pop("idempotency", None)
        if resource:
            resource[0].complete(resource[1])

    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
        """负责“异常发生时的 Hook 错误处理”。

        该函数是 `agent.hooks.idempotency` 中的`IdempotencyHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        if scope != "agent":
            return
        resource = context.resources.pop("idempotency", None)
        if resource:
            resource[0].fail(resource[1])
