"""文件作用：数据库过载快照。

项目关系：本文件依赖 `core.settings`、`infrastructure.database`；被 `apps.api`。
"""



from __future__ import annotations

from dataclasses import asdict, dataclass

from core.settings import database_pool_budget
from infrastructure.database import get_engine


@dataclass(frozen=True)
class OverloadSnapshot:
    """类功能：`OverloadSnapshot` 封装与“数据库过载快照”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    state: str
    checked_out: int
    local_capacity: int

    def to_dict(self) -> dict[str, int | str]:
        """函数功能：`OverloadSnapshot.to_dict` 在类 `OverloadSnapshot` 中负责转换为目标结构 dict，服务于本文件职责：数据库过载快照。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, int | str]`，表示结构化结果、载荷或状态映射。
        """
        return asdict(self)


def database_overload_snapshot() -> OverloadSnapshot:
    """函数功能：`database_overload_snapshot` 负责处理 database overload snapshot，服务于本文件职责：数据库过载快照。
    传参：
        无。
    返回结果说明：
        返回 `OverloadSnapshot` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    engine = get_engine()
    checked_out = int(engine.pool.checkedout()) if hasattr(engine.pool, "checkedout") else 0
    pool_size, max_overflow = database_pool_budget()
    capacity = max(1, pool_size + max_overflow)
    ratio = checked_out / capacity
    state = "overload" if ratio >= 0.9 else "degraded" if ratio >= 0.7 else "normal"
    return OverloadSnapshot(state, checked_out, capacity)
