"""Best-effort Redis session loading and persistence."""

from __future__ import annotations

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from core.settings import COORDINATION_BACKEND
from infrastructure.redis_session import RedisSessionStore


class SessionHook(AgentHook):
    name = "session"

    def before_agent(self, context: AgentRunContext) -> None:
        if COORDINATION_BACKEND != "redis":
            return
        try:
            context.session = RedisSessionStore().get(context.tenant_id, context.user_id)
        except Exception:
            context.session = {}

    def after_agent(self, context: AgentRunContext, result: object) -> None:
        if COORDINATION_BACKEND != "redis":
            return
        try:
            store = RedisSessionStore()
            update = context.metadata.get("session_update")
            if update is None:
                if context.session:
                    store.touch(context.tenant_id, context.user_id)
                return
            if update:
                store.set(context.tenant_id, context.user_id, dict(update))
            else:
                store.delete(context.tenant_id, context.user_id)
        except Exception:
            return
