"""Versioned best-effort caching for read-only tools."""

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
    name = "tool_cache"

    def before_tool(self, context: AgentRunContext, tool_name: str, args: dict[str, Any]) -> None:
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
