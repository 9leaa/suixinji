"""文件作用：过期策略。

项目关系：本文件依赖 `memory.repository`；被 `memory.scheduler`、`tests.test_memory_stage1_correctness`。
"""



from __future__ import annotations

from typing import Any

from memory.repository import expire_due_memories


def run_expiry_once(*, space_id: str | None = None, limit: int = 500) -> dict[str, Any]:
    """函数功能：`run_expiry_once` 负责运行 expiry once，服务于本文件职责：过期策略。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `500`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    expired = expire_due_memories(space_id, limit=limit)
    return {"space_id": space_id, "expired_count": expired, "status": "completed"}
