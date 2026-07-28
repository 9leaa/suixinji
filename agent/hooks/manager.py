"""Registration-order before hooks and reverse-order cleanup hooks."""

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
    def __init__(self, hooks: list[AgentHook] | None = None) -> None:
        """初始化`HookManager` 实例并建立后续调用所需的状态。"""
        self.hooks = list(hooks or [])

    def register(self, hook: AgentHook) -> None:
        """负责“register”。

        该函数是 `agent.hooks.manager` 中的`HookManager` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        self.hooks.append(hook)

    def run_agent(self, context: AgentRunContext, callable_: Callable[[], T]) -> T:
        """负责“运行Agent”。

        该函数是 `agent.hooks.manager` 中的`HookManager` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
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
        """负责“运行LLM”。

        该函数是 `agent.hooks.manager` 中的`HookManager` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
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
        """负责“运行工具”。

        该函数是 `agent.hooks.manager` 中的`HookManager` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
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
        """负责“错误”。

        该函数是 `agent.hooks.manager` 中的`HookManager` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        for hook in reversed(self.hooks):
            try:
                hook.on_error(context, error, scope)
            except Exception:
                continue


def _build_default_manager() -> HookManager:
    """负责“构建默认manager”。

    该函数是 `agent.hooks.manager` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
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
    """负责“获取默认hookmanager”。

    该函数是 `agent.hooks.manager` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    global _default_manager
    if _default_manager is None:
        with _default_lock:
            if _default_manager is None:
                _default_manager = _build_default_manager()
    return _default_manager


def set_default_hook_manager(manager: HookManager | None) -> None:
    """负责“设置默认hookmanager”。

    该函数是 `agent.hooks.manager` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    global _default_manager
    _default_manager = manager
