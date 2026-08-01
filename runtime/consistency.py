"""文件作用：运行时一致性工具。

项目关系：本文件依赖 无直接本地模块依赖；被 `apps.receiver`、`repositories.postgres.dispatch`。
"""



from __future__ import annotations

from typing import Any


MEMORY_QUERY_MARKERS = (
    "喜欢",
    "讨厌",
    "偏好",
    "习惯",
    "过敏",
    "当前待办",
    "现在的任务",
    "待办是什么",
    "任务进度",
    "做到哪了",
    "有进展吗",
    "完成了吗",
    "换得怎么样",
    "弄好了吗",
    "住在哪里",
    "住哪",
    "现在住",
    "正在学习",
    "当前项目",
    "what do i like",
    "preference",
    "todo",
    "current task",
    "where do i live",
)
NOTE_QUERY_MARKERS = (
    "最近",
    "笔记",
    "记录",
    "记了",
    "写了",
    "/type",
    "/tag",
    "标签",
    "类型",
    "recent",
    "record",
    "note",
)


def query_consistency(question: str) -> str:
    """函数功能：`query_consistency` 负责查询 consistency，服务于本文件职责：运行时一致性工具。
    传参：
        question: 用户问题文本，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    normalized = " ".join(str(question or "").strip().casefold().split())
    if any(marker in normalized for marker in MEMORY_QUERY_MARKERS):
        return "memory"
    if any(marker in normalized for marker in NOTE_QUERY_MARKERS):
        return "note"
    return "weak"


def task_consistency(task_type: str, payload: dict[str, Any]) -> str:
    """函数功能：`task_consistency` 负责处理 task consistency，服务于本文件职责：运行时一致性工具。
    传参：
        task_type: task type 参数，由调用方传入，类型为 `str`。
        payload: 结构化载荷，通常来自事件、任务或 API 请求，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    explicit = str(payload.get("consistency") or "").strip().lower()
    if explicit in {"note", "memory", "weak"}:
        return explicit
    if task_type == "ingest" or task_type == "summary":
        return "note"
    if task_type == "query":
        return query_consistency(str(payload.get("question") or ""))
    return "weak"
