"""文件作用：抽取结果 schema。

项目关系：本文件依赖 无直接本地模块依赖；被 `memory.extractor`。
"""



from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class ExtractedMemoryCandidate(BaseModel):
    """类功能：`ExtractedMemoryCandidate` 封装与“抽取结果 schema”相关的数据结构、状态或行为。
    继承关系：继承 `BaseModel`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    memory_type: Literal["task", "semantic", "preference", "episodic"]
    entity: str | None = None
    attribute: str | None = None
    operation: str | None = None
    canonical_topic: str = ""
    task_status: Literal["todo", "in_progress", "blocked", "done", "cancelled"] | None = None
    old_value: str | None = None
    new_value: str | None = None
    evidence_span: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    should_store: bool = True
    extraction_reason: str = ""
    content: str | None = None
    entities: list[str] = Field(default_factory=list)

    @field_validator("entity", "attribute", "operation", "canonical_topic", "old_value", "new_value", "content")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        """函数功能：`ExtractedMemoryCandidate._strip_optional_text` 在类 `ExtractedMemoryCandidate` 中负责处理 strip optional text，服务于本文件职责：抽取结果 schema。
        传参：
            value: 待转换、校验或计算的值，类型为 `str | None`。
        返回结果说明：
            返回 `str | None`；未命中或无需处理时可返回 `None`。
        """
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None

    @model_validator(mode="after")
    def _validate_task_identity(self) -> "ExtractedMemoryCandidate":
        """函数功能：`ExtractedMemoryCandidate._validate_task_identity` 在类 `ExtractedMemoryCandidate` 中负责校验 task identity，服务于本文件职责：抽取结果 schema。
        传参：
            无。
        返回结果说明：
            返回 `'ExtractedMemoryCandidate'` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        if self.memory_type == "task" and self.should_store:
            if not all((self.entity, self.attribute, self.operation, self.task_status, self.canonical_topic)):
                raise ValueError("task candidates require entity, attribute, operation, canonical_topic, and task_status")
        if self.task_status is not None and self.memory_type != "task":
            raise ValueError("task_status is only valid for task candidates")
        return self

    def evidence_is_grounded(self, source_text: str) -> bool:
        """函数功能：`ExtractedMemoryCandidate.evidence_is_grounded` 在类 `ExtractedMemoryCandidate` 中负责判断是否为 grounded，服务于本文件职责：抽取结果 schema。
        传参：
            source_text: source text 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        return bool(self.evidence_span and self.evidence_span in source_text)


def parse_extracted_candidate(row: object, source_text: str) -> ExtractedMemoryCandidate | None:
    """函数功能：`parse_extracted_candidate` 负责解析 extracted candidate，服务于本文件职责：抽取结果 schema。
    传参：
        row: row 参数，由调用方传入，类型为 `object`。
        source_text: source text 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `ExtractedMemoryCandidate | None`；未命中或无需处理时可返回 `None`。
    """
    if not isinstance(row, dict):
        return None
    try:
        candidate = ExtractedMemoryCandidate.model_validate(row)
    except ValidationError:
        return None
    return candidate if candidate.evidence_is_grounded(source_text) else None
