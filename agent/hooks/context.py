"""Shared context carried through Agent and tool hook lifecycles."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from memory.models import new_id


@dataclass
class AgentRunContext:
    run_id: str
    tenant_id: str
    user_id: str
    space_id: str
    message_id: str | None
    task_id: str | None
    trace_id: str | None
    run_type: str
    session: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now().astimezone())

    @classmethod
    def create(
        cls,
        *,
        space_id: str,
        run_type: str,
        tenant_id: str = "default",
        user_id: str | None = None,
        message_id: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "AgentRunContext":
        return cls(
            run_id=new_id("agent"),
            tenant_id=tenant_id or "default",
            user_id=user_id or space_id,
            space_id=space_id,
            message_id=message_id,
            task_id=task_id,
            trace_id=trace_id,
            run_type=run_type,
            metadata=dict(metadata or {}),
        )

    def next_step(self) -> int:
        value = int(self.resources.get("step_no") or 0) + 1
        self.resources["step_no"] = value
        return value
