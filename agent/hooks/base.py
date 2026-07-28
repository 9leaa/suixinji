"""Base no-op hook contract."""

from __future__ import annotations

from typing import Any

from agent.hooks.context import AgentRunContext


class AgentHook:
    name = "base"

    def before_agent(self, context: AgentRunContext) -> None:
        """负责“Agent 执行前的 Hook 前置处理”。

        该函数是 `agent.hooks.base` 中的`AgentHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        pass
    def after_agent(self, context: AgentRunContext, result: Any) -> None:
        """负责“Agent 执行后的 Hook 后置处理”。

        该函数是 `agent.hooks.base` 中的`AgentHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        pass
    def before_llm(self, context: AgentRunContext, request: dict[str, Any]) -> None:
        """负责“LLM 调用前的 Hook 前置处理”。

        该函数是 `agent.hooks.base` 中的`AgentHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        pass
    def after_llm(self, context: AgentRunContext, request: dict[str, Any], result: Any) -> None:
        """负责“LLM 调用后的 Hook 后置处理”。

        该函数是 `agent.hooks.base` 中的`AgentHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        pass
    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None:
        """负责“工具调用前的 Hook 前置处理”。

        该函数是 `agent.hooks.base` 中的`AgentHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        pass
    def after_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any], result: Any) -> None:
        """负责“工具调用后的 Hook 后置处理”。

        该函数是 `agent.hooks.base` 中的`AgentHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        pass
    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
        """负责“异常发生时的 Hook 错误处理”。

        该函数是 `agent.hooks.base` 中的`AgentHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        pass
