"""文件作用：Hook 调度器。

项目关系：本文件依赖 `agent.hooks.base`、`agent.hooks.context`、`agent.hooks.idempotency`、`agent.hooks.llm_usage` 等 11 个模块；被 `agent.hooks.__init__`、`tests.test_agent_hooks`。
"""



from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext

T = TypeVar("T")
_default_manager: "HookManager | None" = None
_default_lock = threading.Lock()


class HookManager:
    """类功能：`HookManager` 封装与“Hook 调度器”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, hooks: list[AgentHook] | None = None) -> None:
        """函数功能：`HookManager.__init__` 在类 `HookManager` 中负责初始化实例状态，服务于本文件职责：Hook 调度器。
        传参：
            hooks: hooks 参数，由调用方传入，类型为 `list[AgentHook] | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.hooks = list(hooks or [])

    def register(self, hook: AgentHook) -> None:
        """函数功能：`HookManager.register` 在类 `HookManager` 中负责处理 register，服务于本文件职责：Hook 调度器。
        传参：
            hook: hook 参数，由调用方传入，类型为 `AgentHook`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.hooks.append(hook)

    def run_agent(self, context: AgentRunContext, callable_: Callable[[], T]) -> T:
        """函数功能：`HookManager.run_agent` 在类 `HookManager` 中负责运行 agent，服务于本文件职责：Hook 调度器。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            callable_: callable  参数，由调用方传入，类型为 `Callable[[], T]`。
        返回结果说明：
            返回 `T` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        try:
            for hook in self.hooks:
                hook.before_agent(context)
            result = callable_()
            for hook in reversed(self.hooks):
                hook.after_agent(context, result)
            return result
        except Exception as exc:
            self._on_error(context, exc, "agent")
            raise

    def run_llm(self, context: AgentRunContext, request: dict[str, Any], callable_: Callable[[], T]) -> T:
        """函数功能：`HookManager.run_llm` 在类 `HookManager` 中负责运行 llm，服务于本文件职责：Hook 调度器。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            request: 请求对象或请求载荷，类型为 `dict[str, Any]`。
            callable_: callable  参数，由调用方传入，类型为 `Callable[[], T]`。
        返回结果说明：
            返回 `T` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        try:
            for hook in self.hooks:
                hook.before_llm(context, request)
            result = callable_()
            for hook in reversed(self.hooks):
                hook.after_llm(context, request, result)
            return result
        except Exception as exc:
            self._on_error(context, exc, "llm")
            raise

    def run_tool(
        self,
        context: AgentRunContext,
        tool_name: str,
        args: dict[str, Any],
        callable_: Callable[[], T],
    ) -> T:
        """函数功能：`HookManager.run_tool` 在类 `HookManager` 中负责运行 tool，服务于本文件职责：Hook 调度器。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
            callable_: callable  参数，由调用方传入，类型为 `Callable[[], T]`。
        返回结果说明：
            返回 `T` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        cache_marker = f"tool_cache_hit:{tool_name}"
        try:
            for hook in self.hooks:
                hook.before_tool(context, tool_name, args)
            if cache_marker in context.resources:
                result = context.resources.pop(cache_marker)
            else:
                result = callable_()
            for hook in reversed(self.hooks):
                hook.after_tool(context, tool_name, args, result)
            return result
        except Exception as exc:
            self._on_error(context, exc, "tool")
            raise

    def _on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
        """函数功能：`HookManager._on_error` 在类 `HookManager` 中负责处理 on error，服务于本文件职责：Hook 调度器。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            error: 当前捕获的异常对象，类型为 `Exception`。
            scope: scope 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        for hook in reversed(self.hooks):
            try:
                hook.on_error(context, error, scope)
            except Exception:
                continue


def _build_default_manager() -> HookManager:
    """函数功能：`_build_default_manager` 负责构建 default manager，服务于本文件职责：Hook 调度器。
    传参：
        无。
    返回结果说明：
        返回 `HookManager` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    from core.settings import AGENT_HOOKS_ENABLED

    if not AGENT_HOOKS_ENABLED:
        return HookManager()
    from agent.hooks.idempotency import IdempotencyHook
    from agent.hooks.llm_usage import LlmUsageHook
    from agent.hooks.observability import ObservabilityHook
    from agent.hooks.rate_limit import RateLimitHook
    from agent.hooks.session import SessionHook
    from agent.hooks.space_lock import SpaceLockHook
    from agent.hooks.task_dispatch import TaskDispatchHook
    from agent.hooks.tool_cache import ToolCacheHook

    return HookManager([
        ObservabilityHook(),
        RateLimitHook(),
        IdempotencyHook(),
        SessionHook(),
        LlmUsageHook(),
        TaskDispatchHook(),
        ToolCacheHook(),
        SpaceLockHook(),
    ])


def get_default_hook_manager() -> HookManager:
    """函数功能：`get_default_hook_manager` 负责获取 default hook manager，服务于本文件职责：Hook 调度器。
    传参：
        无。
    返回结果说明：
        返回 `HookManager` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    global _default_manager
    if _default_manager is None:
        with _default_lock:
            if _default_manager is None:
                _default_manager = _build_default_manager()
    return _default_manager


def set_default_hook_manager(manager: HookManager | None) -> None:
    """函数功能：`set_default_hook_manager` 负责设置 default hook manager，服务于本文件职责：Hook 调度器。
    传参：
        manager: manager 参数，由调用方传入，类型为 `HookManager | None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    global _default_manager
    _default_manager = manager
