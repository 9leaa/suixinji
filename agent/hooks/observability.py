"""文件作用：Agent 观测 Hook。

项目关系：本文件依赖 `agent.hooks.base`、`agent.hooks.context`、`core.observability`、`repositories.postgres.agent_runs`；被 `agent.hooks.manager`。
"""



from __future__ import annotations

import time
from typing import Any

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.observability import log_event
from repositories.postgres.agent_runs import add_agent_step, finish_agent_run, start_agent_run


class ObservabilityHook(AgentHook):
    """类功能：`ObservabilityHook` 封装与“Agent 观测 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `AgentHook`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    name = "observability"

    def before_agent(self, context: AgentRunContext) -> None:
        """函数功能：`ObservabilityHook.before_agent` 在类 `ObservabilityHook` 中负责处理 before agent，服务于本文件职责：Agent 观测 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        context.resources["agent_started_monotonic"] = time.monotonic()
        try:
            start_agent_run(
                context.run_id,
                tenant_id=context.tenant_id,
                space_id=context.space_id,
                user_id=context.user_id,
                message_id=context.message_id,
                run_type=context.run_type,
                started_at=context.started_at,
            )
        except Exception:
            pass
        log_event("agent.before_agent", space_id=context.space_id, message_id=context.message_id, record_id=context.run_id, extra={"run_type": context.run_type})

    def after_agent(self, context: AgentRunContext, result: Any) -> None:
        """函数功能：`ObservabilityHook.after_agent` 在类 `ObservabilityHook` 中负责处理 after agent，服务于本文件职责：Agent 观测 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            result: 上游步骤返回的结果对象，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        try:
            finish_agent_run(context.run_id, "completed")
        except Exception:
            pass
        log_event("agent.after_agent", space_id=context.space_id, message_id=context.message_id, record_id=context.run_id, extra={"run_type": context.run_type})

    def before_llm(self, context: AgentRunContext, request: dict[str, Any]) -> None:
        """函数功能：`ObservabilityHook.before_llm` 在类 `ObservabilityHook` 中负责处理 before llm，服务于本文件职责：Agent 观测 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            request: 请求对象或请求载荷，类型为 `dict[str, Any]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        context.resources["llm_started_monotonic"] = time.monotonic()

    def after_llm(self, context: AgentRunContext, request: dict[str, Any], result: Any) -> None:
        """函数功能：`ObservabilityHook.after_llm` 在类 `ObservabilityHook` 中负责处理 after llm，服务于本文件职责：Agent 观测 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            request: 请求对象或请求载荷，类型为 `dict[str, Any]`。
            result: 上游步骤返回的结果对象，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._step(context, "llm", str(request.get("name") or "complete_json"), "completed", started_key="llm_started_monotonic")

    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None:
        """函数功能：`ObservabilityHook.before_tool` 在类 `ObservabilityHook` 中负责处理 before tool，服务于本文件职责：Agent 观测 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        context.resources[f"tool_started:{tool_name}"] = time.monotonic()

    def after_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any], result: Any) -> None:
        """函数功能：`ObservabilityHook.after_tool` 在类 `ObservabilityHook` 中负责处理 after tool，服务于本文件职责：Agent 观测 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
            result: 上游步骤返回的结果对象，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        safe_input = {"arg_keys": sorted(args), "arg_count": len(args)}
        safe_output = {"result_type": type(result).__name__, "result_count": len(result) if isinstance(result, (list, dict)) else None}
        self._step(context, "tool", tool_name, "completed", started_key=f"tool_started:{tool_name}", safe_input=safe_input, safe_output=safe_output)

    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
        """函数功能：`ObservabilityHook.on_error` 在类 `ObservabilityHook` 中负责处理 on error，服务于本文件职责：Agent 观测 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            error: 当前捕获的异常对象，类型为 `Exception`。
            scope: scope 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if scope == "agent":
            try:
                finish_agent_run(context.run_id, "failed", error_type=type(error).__name__)
            except Exception:
                pass
        else:
            self._step(context, scope, scope, "failed", error_type=type(error).__name__)
        log_event(
            f"agent.{scope}_error",
            level="error",
            status="failed",
            space_id=context.space_id,
            message_id=context.message_id,
            record_id=context.run_id,
            error=type(error).__name__,
        )

    @staticmethod
    def _step(
        context: AgentRunContext,
        step_type: str,
        name: str,
        status: str,
        *,
        started_key: str | None = None,
        safe_input: dict[str, Any] | None = None,
        safe_output: dict[str, Any] | None = None,
        error_type: str | None = None,
    ) -> None:
        """函数功能：`ObservabilityHook._step` 在类 `ObservabilityHook` 中负责处理 step，服务于本文件职责：Agent 观测 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            step_type: step type 参数，由调用方传入，类型为 `str`。
            name: name 参数，由调用方传入，类型为 `str`。
            status: status 参数，由调用方传入，类型为 `str`。
            started_key: started key 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
            safe_input: safe input 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
            safe_output: safe output 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
            error_type: error type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        duration_ms = None
        if started_key:
            started = context.resources.pop(started_key, None)
            if started is not None:
                duration_ms = int((time.monotonic() - started) * 1000)
        try:
            add_agent_step(
                context.run_id,
                context.next_step(),
                step_type=step_type,
                name=name,
                status=status,
                duration_ms=duration_ms,
                safe_input=safe_input,
                safe_output=safe_output,
                error_type=error_type,
            )
        except Exception:
            return
