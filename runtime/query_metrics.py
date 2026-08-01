"""文件作用：查询指标。

项目关系：本文件依赖 无直接本地模块依赖；被 `eval.benchmark_stage2_queries`、`tests.test_postgres_repositories`、`tests.test_query_metrics`。
"""



from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from sqlalchemy import Engine, event


@dataclass
class QueryStats:
    """类功能：`QueryStats` 封装与“查询指标”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """

    count: int = 0
    failed: int = 0
    total_duration_ms: float = 0.0
    durations_ms: list[float] = field(default_factory=list)

    def observe(self, duration_ms: float, *, failed: bool = False) -> None:
        """函数功能：`QueryStats.observe` 在类 `QueryStats` 中负责处理 observe，服务于本文件职责：查询指标。
        传参：
            duration_ms: duration ms 参数，由调用方传入，类型为 `float`。
            failed: failed 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.count += 1
        self.failed += int(failed)
        self.total_duration_ms += duration_ms
        self.durations_ms.append(duration_ms)

    def to_dict(self) -> dict[str, Any]:
        """函数功能：`QueryStats.to_dict` 在类 `QueryStats` 中负责转换为目标结构 dict，服务于本文件职责：查询指标。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        ordered = sorted(self.durations_ms)

        def percentile(ratio: float) -> float | None:
            """函数功能：`QueryStats.percentile` 在类 `QueryStats` 中负责处理 percentile，服务于本文件职责：查询指标。
            传参：
                ratio: ratio 参数，由调用方传入，类型为 `float`。
            返回结果说明：
                返回 `float | None`；未命中或无需处理时可返回 `None`。
            """
            if not ordered:
                return None
            index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
            return round(ordered[index], 3)

        return {
            "count": self.count,
            "failed": self.failed,
            "total_duration_ms": round(self.total_duration_ms, 3),
            "p50_duration_ms": percentile(0.50),
            "p95_duration_ms": percentile(0.95),
            "p99_duration_ms": percentile(0.99),
        }


@contextmanager
def capture_sql_queries(engine: Engine) -> Iterator[QueryStats]:
    """函数功能：`capture_sql_queries` 负责处理 capture sql queries，服务于本文件职责：查询指标。
    传参：
        engine: engine 参数，由调用方传入，类型为 `Engine`。
    返回结果说明：
        返回 `Iterator[QueryStats]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """

    stats = QueryStats()
    started: dict[int, float] = {}

    def before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        """函数功能：`before_cursor_execute` 负责执行 before cursor，服务于本文件职责：查询指标。
        传参：
            conn: 数据库或 Redis 连接对象，类型为 `Any`。
            cursor: cursor 参数，由调用方传入，类型为 `Any`。
            statement: statement 参数，由调用方传入，类型为 `str`。
            parameters: parameters 参数，由调用方传入，类型为 `Any`。
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `Any`。
            executemany: executemany 参数，由调用方传入，类型为 `bool`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        started[id(context)] = time.perf_counter()

    def after_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        """函数功能：`after_cursor_execute` 负责执行 after cursor，服务于本文件职责：查询指标。
        传参：
            conn: 数据库或 Redis 连接对象，类型为 `Any`。
            cursor: cursor 参数，由调用方传入，类型为 `Any`。
            statement: statement 参数，由调用方传入，类型为 `str`。
            parameters: parameters 参数，由调用方传入，类型为 `Any`。
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `Any`。
            executemany: executemany 参数，由调用方传入，类型为 `bool`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        begin = started.pop(id(context), None)
        stats.observe((time.perf_counter() - begin) * 1000 if begin is not None else 0.0)

    def handle_error(exception_context: Any) -> None:
        """函数功能：`handle_error` 负责处理 error，服务于本文件职责：查询指标。
        传参：
            exception_context: exception context 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        context = exception_context.execution_context
        begin = started.pop(id(context), None) if context is not None else None
        stats.observe((time.perf_counter() - begin) * 1000 if begin is not None else 0.0, failed=True)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    event.listen(engine, "after_cursor_execute", after_cursor_execute)
    event.listen(engine, "handle_error", handle_error)
    try:
        yield stats
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
        event.remove(engine, "after_cursor_execute", after_cursor_execute)
        event.remove(engine, "handle_error", handle_error)
