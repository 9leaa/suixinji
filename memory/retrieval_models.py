"""文件作用：Memory 检索 DTO。

项目关系：本文件依赖 `memory.models`；被 `repositories.postgres.memory`。
"""



from __future__ import annotations

from dataclasses import dataclass, field

from memory.models import MemoryRecord


@dataclass
class MemoryRetrievalHit:
    """类功能：`MemoryRetrievalHit` 封装与“Memory 检索 DTO”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    memory: MemoryRecord
    exact_rank: int | None = None
    structured_rank: int | None = None
    fts_rank: int | None = None
    trigram_rank: int | None = None
    vector_rank: int | None = None
    exact_score: float = 0.0
    structured_score: float = 0.0
    fts_score: float = 0.0
    trigram_score: float = 0.0
    vector_score: float = 0.0
    rrf_score: float = 0.0
    policy_score: float = 0.0
    final_score: float = 0.0
    reasons: list[str] = field(default_factory=list)
