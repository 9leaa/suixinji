"""文件作用：空间锁 Hook。

项目关系：本文件依赖 `agent.hooks.base`、`agent.hooks.context`、`infrastructure.redis_keys`、`infrastructure.redis_lock`；被 `agent.hooks.manager`。
"""



from __future__ import annotations

from typing import Any

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from infrastructure.redis_keys import KEYS
from infrastructure.redis_lock import coordinated_lock

WRITE_TOOLS = {"save_note", "update_memory", "forget_memory", "purge_memory", "consolidate_memory", "process_memory"}


class SpaceLockHook(AgentHook):
    """类功能：`SpaceLockHook` 封装与“空间锁 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `AgentHook`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    name = "space_lock"

    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None:
        """函数功能：`SpaceLockHook.before_tool` 在类 `SpaceLockHook` 中负责处理 before tool，服务于本文件职责：空间锁 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if tool_name not in WRITE_TOOLS:
            return
        manager = coordinated_lock(KEYS.lock_space(context.tenant_id, context.space_id), critical=True)
        source = manager.__enter__()
        context.resources[f"space_lock:{tool_name}"] = manager
        context.metadata["lock_source"] = source

    def after_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any], result: Any) -> None:
        """函数功能：`SpaceLockHook.after_tool` 在类 `SpaceLockHook` 中负责处理 after tool，服务于本文件职责：空间锁 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
            result: 上游步骤返回的结果对象，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._release(context, tool_name)

    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
        """函数功能：`SpaceLockHook.on_error` 在类 `SpaceLockHook` 中负责处理 on error，服务于本文件职责：空间锁 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            error: 当前捕获的异常对象，类型为 `Exception`。
            scope: scope 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        for key in [key for key in context.resources if key.startswith("space_lock:")]:
            self._release(context, key.split(":", 1)[1])

    @staticmethod
    def _release(context: AgentRunContext, tool_name: str) -> None:
        """函数功能：`SpaceLockHook._release` 在类 `SpaceLockHook` 中负责释放，服务于本文件职责：空间锁 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        manager = context.resources.pop(f"space_lock:{tool_name}", None)
        if manager is not None:
            manager.__exit__(None, None, None)
