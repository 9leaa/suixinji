"""Persist safe lifecycle events without storing full prompts or user text."""

from __future__ import annotations

import time
from typing import Any

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.observability import log_event
from repositories.postgres.agent_runs import add_agent_step, finish_agent_run, start_agent_run


class ObservabilityHook(AgentHook):
    name = "observability"

    def before_agent(self, context: AgentRunContext) -> None:
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
        try:
            finish_agent_run(context.run_id, "completed")
        except Exception:
            pass
        log_event("agent.after_agent", space_id=context.space_id, message_id=context.message_id, record_id=context.run_id, extra={"run_type": context.run_type})

    def before_llm(self, context: AgentRunContext, request: dict[str, Any]) -> None:
        context.resources["llm_started_monotonic"] = time.monotonic()

    def after_llm(self, context: AgentRunContext, request: dict[str, Any], result: Any) -> None:
        self._step(context, "llm", str(request.get("name") or "complete_json"), "completed", started_key="llm_started_monotonic")

    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None:
        context.resources[f"tool_started:{tool_name}"] = time.monotonic()

    def after_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any], result: Any) -> None:
        safe_input = {"arg_keys": sorted(args), "arg_count": len(args)}
        safe_output = {"result_type": type(result).__name__, "result_count": len(result) if isinstance(result, (list, dict)) else None}
        self._step(context, "tool", tool_name, "completed", started_key=f"tool_started:{tool_name}", safe_input=safe_input, safe_output=safe_output)

    def on_error(self, context: AgentRunContext, error: Exception, scope: str) -> None:
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
