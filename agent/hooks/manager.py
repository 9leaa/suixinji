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
        self.hooks = list(hooks or [])

    def register(self, hook: AgentHook) -> None:
        self.hooks.append(hook)

    def run_agent(self, context: AgentRunContext, callable_: Callable[[], T]) -> T:
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
        for hook in reversed(self.hooks):
            try:
                hook.on_error(context, error, scope)
            except Exception:
                continue


def _build_default_manager() -> HookManager:
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
    global _default_manager
    if _default_manager is None:
        with _default_lock:
            if _default_manager is None:
                _default_manager = _build_default_manager()
    return _default_manager


def set_default_hook_manager(manager: HookManager | None) -> None:
    global _default_manager
    _default_manager = manager
