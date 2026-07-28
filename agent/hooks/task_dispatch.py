"""Optional Hook that converts explicitly marked long tools into durable tasks."""

from __future__ import annotations

from typing import Any

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.settings import TASK_QUEUE_BACKEND
from repositories.postgres.dispatch import enqueue_task

ASYNC_TOOLS = {"generate_summary": "summary", "consolidate_memory": "memory", "large_import": "ingest"}


class TaskDispatchHook(AgentHook):
    name = "task_dispatch"

    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None:
        """负责“工具调用前的 Hook 前置处理”。

        该函数是 `agent.hooks.task_dispatch` 中的`TaskDispatchHook` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        if TASK_QUEUE_BACKEND != "redis_streams" or not context.metadata.get("allow_tool_dispatch"):
            return
        task_type = ASYNC_TOOLS.get(tool_name)
        if task_type is None:
            return
        task_id, _ = enqueue_task(
            task_type=task_type,
            tenant_id=context.tenant_id,
            space_id=context.space_id,
            source_message_id=context.message_id,
            idempotency_key=f"tool:{context.run_id}:{tool_name}",
            payload={"tool_name": tool_name, "args": args, "parent_run_id": context.run_id},
        )
        context.resources[f"tool_cache_hit:{tool_name}"] = {"status": "queued", "task_id": task_id}
