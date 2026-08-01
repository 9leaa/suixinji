"""文件作用：工具结果缓存 Hook。

项目关系：本文件依赖 `agent.hooks.base`、`agent.hooks.context`、`core.settings`、`infrastructure.redis_cache`；被 `agent.hooks.manager`。
"""



from __future__ import annotations

import json
from typing import Any

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.settings import CACHE_ENABLED, COORDINATION_BACKEND
from infrastructure.redis_cache import RedisCache

READ_TOOLS = {"memory_search", "semantic_search", "list_recent", "get_note", "follow_links", "by_type", "by_tag", "filter_notes", "related_notes"}
WRITE_TOOLS = {"save_note", "update_memory", "forget_memory", "purge_memory", "consolidate_memory", "process_memory"}


class ToolCacheHook(AgentHook):
    """类功能：`ToolCacheHook` 封装与“工具结果缓存 Hook”相关的数据结构、状态或行为。
    继承关系：继承 `AgentHook`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    name = "tool_cache"

    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None:
        """函数功能：`ToolCacheHook.before_tool` 在类 `ToolCacheHook` 中负责处理 before tool，服务于本文件职责：工具结果缓存 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if COORDINATION_BACKEND != "redis" or not CACHE_ENABLED or tool_name not in READ_TOOLS:
            return
        payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        try:
            value = RedisCache().get(tool_name, context.space_id, payload)
        except Exception:
            return
        if value is not None:
            context.resources[f"tool_cache_hit:{tool_name}"] = value
            context.resources[f"tool_cache_payload:{tool_name}"] = payload

    def after_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any], result: Any) -> None:
        """函数功能：`ToolCacheHook.after_tool` 在类 `ToolCacheHook` 中负责处理 after tool，服务于本文件职责：工具结果缓存 Hook。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext`。
            tool_name: tool name 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
            result: 上游步骤返回的结果对象，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if COORDINATION_BACKEND != "redis" or not CACHE_ENABLED:
            return
        try:
            cache = RedisCache()
            if tool_name in WRITE_TOOLS:
                cache.bump_version(context.space_id)
                return
            if tool_name not in READ_TOOLS:
                return
            payload = context.resources.pop(
                f"tool_cache_payload:{tool_name}",
                json.dumps(args, ensure_ascii=False, sort_keys=True, default=str),
            )
            cache.set(tool_name, context.space_id, payload, result)
        except Exception:
            return
