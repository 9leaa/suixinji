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
        if tool_name not in WRITE_TOOLS:
            return
        manager = coordinated_lock(KEYS.lock_space(context.space_id), critical=True)
        source = manager.__enter__()
        context.resources[f"space_lock:{tool_name}"] = manager
        context.metadata["lock_source"] = source

    def after_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any], result: Any) -> None:
        self._release(context, tool_name)

    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
        for key in [key for key in context.resources if key.startswith("space_lock:")]:
            self._release(context, key.split(":", 1)[1])

    @staticmethod
    def _release(context: AgentRunContext, tool_name: str) -> None:
        manager = context.resources.pop(f"space_lock:{tool_name}", None)
        if manager is not None:
            manager.__exit__(None, None, None)
