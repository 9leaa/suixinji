"""文件作用：Agent 审计数据访问。

项目关系：本文件依赖 `infrastructure.database`、`infrastructure.schema`、`repositories.postgres.common`；被 `agent.hooks.llm_usage`、`agent.hooks.observability`。
"""



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
    """函数功能：`start_agent_run` 负责启动 agent run，服务于本文件职责：Agent 审计数据访问。
    传参：
        run_id: run id 参数，由调用方传入，类型为 `str`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`。
        run_type: run type 参数，由调用方传入，类型为 `str`。
        started_at: started at 参数，由调用方传入，类型为 `datetime`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`finish_agent_run` 负责运行 finish agent，服务于本文件职责：Agent 审计数据访问。
    传参：
        run_id: run id 参数，由调用方传入，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`。
        error_type: error type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`add_agent_step` 负责处理 add agent step，服务于本文件职责：Agent 审计数据访问。
    传参：
        run_id: run id 参数，由调用方传入，类型为 `str`。
        step_no: step no 参数，由调用方传入，类型为 `int`。
        step_type: step type 参数，由调用方传入，类型为 `str`。
        name: name 参数，由调用方传入，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`。
        duration_ms: duration ms 参数，由调用方传入，类型为 `int | None`，默认值为 `None`。
        safe_input: safe input 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
        safe_output: safe output 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
        error_type: error type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`add_llm_usage` 负责处理 add llm usage，服务于本文件职责：Agent 审计数据访问。
    传参：
        run_id: run id 参数，由调用方传入，类型为 `str`。
        model: model 参数，由调用方传入，类型为 `str`。
        input_tokens: input tokens 参数，由调用方传入，类型为 `int`，默认值为 `0`。
        output_tokens: output tokens 参数，由调用方传入，类型为 `int`，默认值为 `0`。
        estimated_cost: estimated cost 参数，由调用方传入，类型为 `Decimal | float`，默认值为 `0`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
