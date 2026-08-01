"""文件作用：事件策略。

项目关系：本文件依赖 无直接本地模块依赖；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

from datetime import datetime


def age_days(created_at: str, *, now: datetime | None = None) -> float:
    """函数功能：`age_days` 负责处理 age days，服务于本文件职责：事件策略。
    传参：
        created_at: created at 参数，由调用方传入，类型为 `str`。
        now: now 参数，由调用方传入，类型为 `datetime | None`，默认值为 `None`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return 0.0
    current = now or datetime.now().astimezone()
    if created.tzinfo is None:
        created = created.replace(tzinfo=current.tzinfo)
    return max(0.0, (current - created).total_seconds() / 86400)


def recency_weight(created_at: str, *, now: datetime | None = None) -> float:
    """函数功能：`recency_weight` 负责处理 recency weight，服务于本文件职责：事件策略。
    传参：
        created_at: created at 参数，由调用方传入，类型为 `str`。
        now: now 参数，由调用方传入，类型为 `datetime | None`，默认值为 `None`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    age = age_days(created_at, now=now)
    return max(0.15, 1.0 - min(age, 365.0) / 365.0)
