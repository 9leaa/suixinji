"""文件作用：候选与既有 Memory 的关系识别。

项目关系：本文件依赖 `memory.adjudicator`、`memory.models`；被 `tests.test_memory_adjudication_evolution`。
"""



from __future__ import annotations

from dataclasses import dataclass

from memory.adjudicator import adjudicate_memory
from memory.models import MemoryCandidate, MemoryRecord


LEGACY_RELATIONS = {
    "new": "new",
    "same": "same",
    "merge": "extend",
    "update_task": "update",
    "supersede": "update",
    "conflict": "contradict",
}


@dataclass(frozen=True)
class RelationDecision:
    """类功能：`RelationDecision` 封装与“候选与既有 Memory 的关系识别”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    relation: str
    action: str
    target_memory_id: str | None = None
    reason: str | None = None


def classify_relation(candidate: MemoryCandidate, memories: list[MemoryRecord]) -> RelationDecision:
    """函数功能：`classify_relation` 负责分类 relation，服务于本文件职责：候选与既有 Memory 的关系识别。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memories: memories 参数，由调用方传入，类型为 `list[MemoryRecord]`。
    返回结果说明：
        返回 `RelationDecision` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    decision = adjudicate_memory(candidate, memories)
    return RelationDecision(
        relation=LEGACY_RELATIONS[decision.relation],
        action=decision.recommended_action,
        target_memory_id=decision.target_memory_ids[0] if decision.target_memory_ids else None,
        reason=decision.reason,
    )
