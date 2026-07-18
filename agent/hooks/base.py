"""Base no-op hook contract."""

from __future__ import annotations

from typing import Any

from agent.hooks.context import AgentRunContext


class AgentHook:
    name = "base"

    def before_agent(self, context: AgentRunContext) -> None: pass
    def after_agent(self, context: AgentRunContext, result: Any) -> None: pass
    def before_llm(self, context: AgentRunContext, request: dict[str, Any]) -> None: pass
    def after_llm(self, context: AgentRunContext, request: dict[str, Any], result: Any) -> None: pass
    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None: pass
    def after_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any], result: Any) -> None: pass
    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None: pass
