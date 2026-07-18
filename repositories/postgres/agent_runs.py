"""PostgreSQL persistence for Agent runs, steps, and LLM usage."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import AgentRun, AgentStep, LlmUsage
from repositories.postgres.common import DEFAULT_TENANT_ID


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
        session.execute(
            insert(AgentRun)
            .values(
                run_id=run_id,
                tenant_id=tenant_id or DEFAULT_TENANT_ID,
                space_id=space_id,
                user_id=user_id or None,
                message_id=message_id,
                run_type=run_type,
                status="running",
                started_at=started_at,
            )
            .on_conflict_do_nothing(index_elements=[AgentRun.run_id])
        )


def finish_agent_run(run_id: str, status: str, *, error_type: str | None = None) -> None:
    with session_scope() as session:
        session.execute(
            update(AgentRun)
            .where(AgentRun.run_id == run_id)
            .values(status=status, finished_at=datetime.now().astimezone(), error_type=error_type)
        )


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
