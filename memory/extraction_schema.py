"""Strict model-output contract for Memory V3 extraction."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class ExtractedMemoryCandidate(BaseModel):
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
        """负责“stripoptional文本”。

        该函数是 `memory.extraction_schema` 中的`ExtractedMemoryCandidate` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None

    @model_validator(mode="after")
    def _validate_task_identity(self) -> "ExtractedMemoryCandidate":
        """负责“校验任务identity”。

        该函数是 `memory.extraction_schema` 中的`ExtractedMemoryCandidate` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        if self.memory_type == "task" and self.should_store:
            if not all((self.entity, self.attribute, self.operation, self.task_status, self.canonical_topic)):
                raise ValueError("task candidates require entity, attribute, operation, canonical_topic, and task_status")
        if self.task_status is not None and self.memory_type != "task":
            raise ValueError("task_status is only valid for task candidates")
        return self

    def evidence_is_grounded(self, source_text: str) -> bool:
        """负责“evidence是否为grounded”。

        该函数是 `memory.extraction_schema` 中的`ExtractedMemoryCandidate` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        return bool(self.evidence_span and self.evidence_span in source_text)


def parse_extracted_candidate(row: object, source_text: str) -> ExtractedMemoryCandidate | None:
    """负责“解析extracted候选”。

    该函数是 `memory.extraction_schema` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    if not isinstance(row, dict):
        return None
    try:
        candidate = ExtractedMemoryCandidate.model_validate(row)
    except ValidationError:
        return None
    return candidate if candidate.evidence_is_grounded(source_text) else None

