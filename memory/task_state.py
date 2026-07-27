"""Deterministic task lifecycle parsing shared by extraction and controls."""

from __future__ import annotations

from memory.models import TASK_STATUSES


def infer_task_status(text: str) -> str | None:
    """Infer an explicit lifecycle state; return None when absent."""
    value = str(text or "")
    if any(token in value for token in ("取消", "不用做", "不做了")):
        return "cancelled"
    if any(token in value for token in ("卡住", "阻塞", "等确认")):
        return "blocked"
    if any(token in value for token in ("正在", "进行中", "继续")):
        return "in_progress"
    if any(token in value for token in ("需要", "记得", "待办", "要做", "准备", "计划")):
        return "todo"
    if any(token in value for token in ("完成", "搞定", "已做完", "做完", "学完", "已学完", "弄好", "弄好了")):
        return "done"
    if "已经" in value and any(token in value for token in ("换成", "更换为", "改成", "替换成", "部署", "发布")):
        return "done"
    if "准备" in value and any(token in value for token in ("最近", "现在", "重点")):
        return "in_progress"
    return None


def validate_task_status(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized not in TASK_STATUSES:
        raise ValueError(f"invalid task_status: {value}")
    return normalized
