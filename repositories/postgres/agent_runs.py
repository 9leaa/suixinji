"""PostgreSQL persistence for Agent runs, steps, and LLM usage."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from infrastructure.database import session_scope
from infrastructure.schema import AgentRun, AgentStep, LlmUsage
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, ensure_user


def start_agent_run(
    run_id: str,
    *,
    tenant_id: str,
    space_id: str,
    user_id: str,
    message_id: str | None,
    run_type: str,
    started_at: datetime,
) -> None:
    with session_scope() as session:
        ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        if user_id:
            ensure_user(session, user_id, tenant_id=tenant_id)
        if session.get(AgentRun, run_id) is None:
            session.add(
                AgentRun(
                    run_id=run_id,
                    tenant_id=tenant_id or DEFAULT_TENANT_ID,
                    space_id=space_id,
                    user_id=user_id or None,
                    message_id=message_id,
                    run_type=run_type,
                    status="running",
                    started_at=started_at,
                )
            )


def finish_agent_run(run_id: str, status: str, *, error_type: str | None = None) -> None:
    with session_scope() as session:
        row = session.execute(select(AgentRun).where(AgentRun.run_id == run_id).with_for_update()).scalar_one_or_none()
        if row is None:
            return
        row.status = status
        row.finished_at = datetime.now().astimezone()
        row.error_type = error_type


def add_agent_step(
    run_id: str,
    step_no: int,
    *,
    step_type: str,
    name: str,
    status: str,
    duration_ms: int | None = None,
    safe_input: dict[str, Any] | None = None,
    safe_output: dict[str, Any] | None = None,
    error_type: str | None = None,
) -> None:
    with session_scope() as session:
        session.add(
            AgentStep(
                run_id=run_id,
                step_no=step_no,
                step_type=step_type,
                name=name,
                status=status,
                duration_ms=duration_ms,
                safe_input_json=safe_input,
                safe_output_json=safe_output,
                error_type=error_type,
            )
        )


def add_llm_usage(
    run_id: str,
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost: Decimal | float = 0,
) -> None:
    with session_scope() as session:
        session.add(
            LlmUsage(
                run_id=run_id,
                model=model,
                request_count=1,
                input_tokens=max(0, int(input_tokens)),
                output_tokens=max(0, int(output_tokens)),
                total_tokens=max(0, int(input_tokens)) + max(0, int(output_tokens)),
                estimated_cost=Decimal(str(estimated_cost)),
            )
        )
