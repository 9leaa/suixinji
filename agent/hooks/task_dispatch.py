"""文件作用：任务投递 Hook。

项目关系：本文件依赖 `agent.hooks.base`、`agent.hooks.context`、`core.settings`、`repositories.postgres.dispatch`；被 `agent.hooks.manager`。
"""



from __future__ import annotations

from typing import Any

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.settings import TASK_QUEUE_BACKEND
from repositories.postgres.dispatch import enqueue_task

ASYNC_TOOLS = {"generate_summary": "summary", "consolidate_memory": "memory", "large_import": "ingest"}


class TaskDispatchHook(AgentHook):
    """类功能：`TaskDispatchHook` 封装与“任务投递 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `AgentHook`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    name = "task_dispatch"

    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None:
        """函数功能：`TaskDispatchHook.before_tool` 在类 `TaskDispatchHook` 中负责处理 before tool，服务于本文件职责：任务投递 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
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
