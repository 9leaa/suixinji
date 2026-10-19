"""The preference extraction contract, shared by extraction and card writes."""
from typing import Literal

from pydantic import BaseModel, Field


class DetailInput(BaseModel):
    subject: str = '用户'
    preference_object: str
    attitude: Literal['喜欢', '不喜欢', '无所谓', '不确定']
    condition: str = ''
    description: str
    evidence_span: str
    antecedent_note_id: str | None = None
    local_antecedent: str | None = None
    unresolved_reason: str | None = None
    is_general: bool | None = None


class PreferenceCandidateInput(DetailInput):
    memory_type: Literal['preference']
    condition: str
    is_general: bool
    confidence: float = Field(default=.9, ge=0, le=1)
    importance: float = Field(default=.6, ge=0, le=1)
    should_store: bool = True
    extraction_reason: str = ''
