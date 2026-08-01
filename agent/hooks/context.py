"""文件作用：单次 Agent 运行上下文。

项目关系：本文件依赖 `memory.models`；被 `agent.hooks.__init__`、`agent.hooks.base`、`agent.hooks.idempotency`、`agent.hooks.llm_usage` 等 12 个模块。
"""



from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from memory.models import new_id


@dataclass
class AgentRunContext:
    """类功能：`AgentRunContext` 封装与“单次 Agent 运行上下文”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
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
        """函数功能：`AgentRunContext.create` 在类 `AgentRunContext` 中负责创建，服务于本文件职责：单次 Agent 运行上下文。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            run_type: run type 参数，由调用方传入，类型为 `str`。
            tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`，默认值为 `'default'`。
            user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str | None`，默认值为 `None`。
            message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
            task_id: 任务标识，用于查询、更新或幂等处理任务状态，类型为 `str | None`，默认值为 `None`。
            trace_id: Trace 标识，用于读取或写入审计链路，类型为 `str | None`，默认值为 `None`。
            metadata: metadata 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
        返回结果说明：
            返回 `'AgentRunContext'` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
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
        """函数功能：`AgentRunContext.next_step` 在类 `AgentRunContext` 中负责生成下一个值 step，服务于本文件职责：单次 Agent 运行上下文。
        传参：
            无。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
        value = int(self.resources.get("step_no") or 0) + 1
        self.resources["step_no"] = value
        return value
