"""文件作用：任务状态归一化与对账。

项目关系：本文件依赖 `memory.models`；被 `memory.extractor`、`memory.repository`、`memory.service`、`repositories.postgres.memory`。
"""



from __future__ import annotations

from memory.models import TASK_STATUSES


def infer_task_status(text: str) -> str | None:
    """函数功能：`infer_task_status` 负责处理 infer task status，服务于本文件职责：任务状态归一化与对账。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    value = str(text or "")
    if any(token in value for token in ("取消", "不用做", "不做了", "放弃")):
        return "done"
    if any(token in value for token in ("卡住", "阻塞", "等确认", "等待权限", "暂停")):
        return "todo"
    if any(token in value for token in ("正在", "进行中", "继续")):
        return "todo"
    if any(token in value for token in ("需要", "记得", "待办", "要做", "准备", "计划")):
        return "todo"
    if any(token in value for token in ("完成", "搞定", "已做完", "做完", "学完", "已学完", "弄好", "弄好了")):
        return "done"
    if "已经" in value and any(token in value for token in ("换成", "更换为", "改成", "替换成", "部署", "发布")):
        return "done"
    if "准备" in value and any(token in value for token in ("最近", "现在", "重点")):
        return "todo"
    return None


def validate_task_status(value: str | None) -> str | None:
    """函数功能：`validate_task_status` 负责校验 task status，服务于本文件职责：任务状态归一化与对账。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    if value is None:
        return None
    normalized = str(value).strip().lower()
    normalized = {"in_progress": "todo", "blocked": "todo", "cancelled": "done", "canceled": "done"}.get(normalized, normalized)
    if normalized not in TASK_STATUSES:
        raise ValueError(f"invalid task_status: {value}")
    return normalized
