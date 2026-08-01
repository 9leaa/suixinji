"""文件作用：指标聚合。

项目关系：本文件依赖 无直接本地模块依赖；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


LOG_DIR = Path("data/logs")
OUTPUT_PATH = Path("docs/metrics/latest.json")


def load_events() -> list[dict[str, Any]]:
    """函数功能：`load_events` 负责加载 events，服务于本文件职责：指标聚合。
    传参：
        无。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    events: list[dict[str, Any]] = []
    if not LOG_DIR.exists():
        return events
    for path in sorted(LOG_DIR.glob("app-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def percentile(values: list[int], ratio: float) -> int | None:
    """函数功能：`percentile` 负责处理 percentile，服务于本文件职责：指标聚合。
    传参：
        values: values 参数，由调用方传入，类型为 `list[int]`。
        ratio: ratio 参数，由调用方传入，类型为 `float`。
    返回结果说明：
        返回 `int | None`；未命中或无需处理时可返回 `None`。
    """
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, round((len(values) - 1) * ratio))
    return values[index]


def build_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """函数功能：`build_metrics` 负责构建 metrics，服务于本文件职责：指标聚合。
    传参：
        events: events 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    success_events = [
        event
        for event in events
        if event.get("action") == "runtime.task_success"
    ]
    failed_events = [
        event
        for event in events
        if event.get("action") == "runtime.task_failed"
    ]
    rejected_events = [
        event
        for event in events
        if event.get("action") == "runtime.task_rejected"
    ]

    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in success_events:
        task_type = str((event.get("extra") or {}).get("task_type") or "")
        by_type.setdefault(task_type, []).append(event)

    def durations(task_type: str, field: str) -> list[int]:
        """函数功能：`durations` 负责处理 durations，服务于本文件职责：指标聚合。
        传参：
            task_type: task type 参数，由调用方传入，类型为 `str`。
            field: field 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `list[int]`，表示按条件筛选、构造或查询得到的列表。
        """
        values = []
        for event in by_type.get(task_type, []):
            value = (event.get("extra") or {}).get(field)
            if isinstance(value, int):
                values.append(value)
        return values

    total = len(success_events) + len(failed_events) + len(rejected_events)
    return {
        "measurement_status": "measured" if total else "not_measured",
        "p50_ingest_latency_ms": percentile(durations("ingest", "total_duration_ms"), 0.5),
        "p95_ingest_latency_ms": percentile(durations("ingest", "total_duration_ms"), 0.95),
        "p50_query_latency_ms": percentile(durations("query", "total_duration_ms"), 0.5),
        "p95_query_latency_ms": percentile(durations("query", "total_duration_ms"), 0.95),
        "p50_queue_wait_ms": percentile(
            [value for task_type in by_type for value in durations(task_type, "queue_wait_ms")],
            0.5,
        ),
        "task_success_rate": round(len(success_events) / total, 4) if total else None,
        "task_rejection_rate": round(len(rejected_events) / total, 4) if total else None,
        "task_count": total,
        "duration_mean_ms": round(statistics.mean(durations("ingest", "total_duration_ms")), 2)
        if durations("ingest", "total_duration_ms")
        else None,
    }


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：指标聚合。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if OUTPUT_PATH.exists():
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    existing.update(build_metrics(load_events()))
    OUTPUT_PATH.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
