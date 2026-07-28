"""Small process-local overload signal for Redis outage backpressure."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from core.settings import database_pool_budget
from infrastructure.database import get_engine


@dataclass(frozen=True)
class OverloadSnapshot:
    state: str
    checked_out: int
    local_capacity: int

    def to_dict(self) -> dict[str, int | str]:
        """负责“转换为dict”。

        该函数是 `infrastructure.overload` 中的`OverloadSnapshot` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return asdict(self)


def database_overload_snapshot() -> OverloadSnapshot:
    """负责“databaseoverloadsnapshot”。

    该函数是 `infrastructure.overload` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    engine = get_engine()
    checked_out = int(engine.pool.checkedout()) if hasattr(engine.pool, "checkedout") else 0
    pool_size, max_overflow = database_pool_budget()
    capacity = max(1, pool_size + max_overflow)
    ratio = checked_out / capacity
    state = "overload" if ratio >= 0.9 else "degraded" if ratio >= 0.7 else "normal"
    return OverloadSnapshot(state, checked_out, capacity)
