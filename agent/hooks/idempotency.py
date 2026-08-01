"""文件作用：查询幂等 Hook。

项目关系：本文件依赖 `agent.hooks.base`、`agent.hooks.context`、`core.settings`、`infrastructure.redis_idempotency` 等 5 个模块；被 `agent.hooks.manager`。
"""



from __future__ import annotations

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.settings import COORDINATION_BACKEND
from infrastructure.redis_idempotency import IdempotencyStore, LOCAL_IDEMPOTENCY
from infrastructure.redis_keys import KEYS


class DuplicateAgentRequest(RuntimeError):
    """类功能：`DuplicateAgentRequest` 封装与“查询幂等 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `RuntimeError`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    pass


class IdempotencyHook(AgentHook):
    """类功能：`IdempotencyHook` 封装与“查询幂等 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `AgentHook`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    name = "idempotency"

    def before_agent(self, context: AgentRunContext) -> None:
        """函数功能：`IdempotencyHook.before_agent` 在类 `IdempotencyHook` 中负责处理 before agent，服务于本文件职责：查询幂等 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
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
        """函数功能：`IdempotencyHook.after_agent` 在类 `IdempotencyHook` 中负责处理 after agent，服务于本文件职责：查询幂等 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            result: 上游步骤返回的结果对象，类型为 `object`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        resource = context.resources.pop("idempotency", None)
        if resource:
            resource[0].complete(resource[1])

    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
        """函数功能：`IdempotencyHook.on_error` 在类 `IdempotencyHook` 中负责处理 on error，服务于本文件职责：查询幂等 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            error: 当前捕获的异常对象，类型为 `Exception`。
            scope: scope 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if scope != "agent":
            return
        resource = context.resources.pop("idempotency", None)
        if resource:
            resource[0].fail(resource[1])
