"""文件作用：Hook 抽象接口。

项目关系：本文件依赖 `agent.hooks.context`；被 `agent.hooks.idempotency`、`agent.hooks.llm_usage`、`agent.hooks.manager`、`agent.hooks.observability` 等 10 个模块。
"""



from __future__ import annotations

from typing import Any

from agent.hooks.context import AgentRunContext


class AgentHook:
    """类功能：`AgentHook` 封装与“Hook 抽象接口”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    name = "base"

    def before_agent(self, context: AgentRunContext) -> None:
        """函数功能：`AgentHook.before_agent` 在类 `AgentHook` 中负责处理 before agent，服务于本文件职责：Hook 抽象接口。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        pass
    def after_agent(self, context: AgentRunContext, result: Any) -> None:
        """函数功能：`AgentHook.after_agent` 在类 `AgentHook` 中负责处理 after agent，服务于本文件职责：Hook 抽象接口。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            result: 上游步骤返回的结果对象，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        pass
    def before_llm(self, context: AgentRunContext, request: dict[str, Any]) -> None:
        """函数功能：`AgentHook.before_llm` 在类 `AgentHook` 中负责处理 before llm，服务于本文件职责：Hook 抽象接口。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            request: 请求对象或请求载荷，类型为 `dict[str, Any]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        pass
    def after_llm(self, context: AgentRunContext, request: dict[str, Any], result: Any) -> None:
        """函数功能：`AgentHook.after_llm` 在类 `AgentHook` 中负责处理 after llm，服务于本文件职责：Hook 抽象接口。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            request: 请求对象或请求载荷，类型为 `dict[str, Any]`。
            result: 上游步骤返回的结果对象，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        pass
    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None:
        """函数功能：`AgentHook.before_tool` 在类 `AgentHook` 中负责处理 before tool，服务于本文件职责：Hook 抽象接口。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        pass
    def after_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any], result: Any) -> None:
        """函数功能：`AgentHook.after_tool` 在类 `AgentHook` 中负责处理 after tool，服务于本文件职责：Hook 抽象接口。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
            result: 上游步骤返回的结果对象，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        pass
    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
        """函数功能：`AgentHook.on_error` 在类 `AgentHook` 中负责处理 on error，服务于本文件职责：Hook 抽象接口。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            error: 当前捕获的异常对象，类型为 `Exception`。
            scope: scope 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        pass
