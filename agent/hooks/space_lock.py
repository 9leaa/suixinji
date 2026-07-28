"""Short critical-section lock hook for mutating tools."""

from __future__ import annotations

from typing import Any

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from infrastructure.redis_keys import KEYS
from infrastructure.redis_lock import coordinated_lock

WRITE_TOOLS = {"save_note", "update_memory", "forget_memory", "purge_memory", "consolidate_memory", "process_memory"}


class SpaceLockHook(AgentHook):
    name = "space_lock"

    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None:
        """负责“工具调用前的 Hook 前置处理”。

        该函数是 `agent.hooks.space_lock` 中的`SpaceLockHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        if tool_name not in WRITE_TOOLS:
            return
        manager = coordinated_lock(KEYS.lock_space(context.tenant_id, context.space_id), critical=True)
        source = manager.__enter__()
        context.resources[f"space_lock:{tool_name}"] = manager
        context.metadata["lock_source"] = source

    def after_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any], result: Any) -> None:
        """负责“工具调用后的 Hook 后置处理”。

        该函数是 `agent.hooks.space_lock` 中的`SpaceLockHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        self._release(context, tool_name)

    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
        """负责“异常发生时的 Hook 错误处理”。

        该函数是 `agent.hooks.space_lock` 中的`SpaceLockHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        for key in [key for key in context.resources if key.startswith("space_lock:")]:
            self._release(context, key.split(":", 1)[1])

    @staticmethod
    def _release(context: AgentRunContext, tool_name: str) -> None:
        """负责“释放”。

        该函数是 `agent.hooks.space_lock` 中的`SpaceLockHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        manager = context.resources.pop(f"space_lock:{tool_name}", None)
        if manager is not None:
            manager.__exit__(None, None, None)
