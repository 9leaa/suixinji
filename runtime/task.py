"""文件作用：本地任务数据模型/状态常量。

项目关系：本文件依赖 无直接本地模块依赖；被 `bot.feishu_bot`、`runtime.delivery_store`、`runtime.executor`、`runtime.pending_drainer` 等 15 个模块。
"""



from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4


TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_SUCCESS = "success"
TASK_FAILED = "failed"
TASK_REJECTED = "rejected"


def now_iso() -> str:
    """函数功能：`now_iso` 负责获取当前时间 iso，服务于本文件职责：本地任务数据模型/状态常量。
    传参：
        无。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass
class Task:
    """类功能：`Task` 封装与“本地任务数据模型/状态常量”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    id: str
    task_type: str
    space_id: str
    message_id: str | None
    payload: dict[str, Any]
    status: str = TASK_QUEUED
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    queue_wait_ms: int | None = None
    execution_ms: int | None = None
    total_duration_ms: int | None = None


def create_task(
    task_type: str,
    space_id: str,
    payload: dict[str, Any],
    *,
    message_id: str | None = None,
    status: str = TASK_QUEUED,
) -> Task:
    """函数功能：`create_task` 负责创建 task，服务于本文件职责：本地任务数据模型/状态常量。
    传参：
        task_type: task type 参数，由调用方传入，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        payload: 结构化载荷，通常来自事件、任务或 API 请求，类型为 `dict[str, Any]`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `TASK_QUEUED`。
    返回结果说明：
        返回 `Task` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return Task(
        id=str(uuid4()),
        task_type=task_type,
        space_id=space_id,
        message_id=message_id,
        payload=payload,
        status=status,
    )
