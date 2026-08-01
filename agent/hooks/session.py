"""文件作用：会话 Hook。

项目关系：本文件依赖 `agent.hooks.base`、`agent.hooks.context`、`core.settings`、`infrastructure.redis_session`；被 `agent.hooks.manager`。
"""



from __future__ import annotations

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.settings import COORDINATION_BACKEND
from infrastructure.redis_session import RedisSessionStore


class SessionHook(AgentHook):
    """类功能：`SessionHook` 封装与“会话 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `AgentHook`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    name = "session"

    def before_agent(self, context: AgentRunContext) -> None:
        """函数功能：`SessionHook.before_agent` 在类 `SessionHook` 中负责处理 before agent，服务于本文件职责：会话 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if COORDINATION_BACKEND != "redis":
            return
        try:
            context.session = RedisSessionStore().get(context.tenant_id, context.user_id)
        except Exception:
            context.session = {}

    def after_agent(self, context: AgentRunContext, result: object) -> None:
        """函数功能：`SessionHook.after_agent` 在类 `SessionHook` 中负责处理 after agent，服务于本文件职责：会话 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            result: 上游步骤返回的结果对象，类型为 `object`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if COORDINATION_BACKEND != "redis":
            return
        try:
            store = RedisSessionStore()
            update = context.metadata.get("session_update")
            if update is None:
                if context.session:
                    store.touch(context.tenant_id, context.user_id)
                return
            if update:
                store.set(context.tenant_id, context.user_id, dict(update))
            else:
                store.delete(context.tenant_id, context.user_id)
        except Exception:
            return
