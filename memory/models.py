"""Shared models and constants for the core memory system."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MEMORY_TYPES = {"episodic", "semantic", "preference", "task"}
MEMORY_STATUSES = {
    "active",
    "superseded",
    "conflicted",
    "forgotten",
    "archived",
    "pending_review",
    # Kept for compatibility with the existing public commands.
    "deleted",
    "expired",
}
TASK_STATUSES = {"todo", "in_progress", "blocked", "done", "cancelled"}
SOURCE_RELATIONS = {"created_from", "supported_by", "updated_by", "contradicted_by", "summarized_from"}
DECISION_RELATIONS = {"new", "same", "merge", "update_task", "supersede", "conflict"}
DECISION_ACTIONS = {"insert", "add_source", "merge", "update_task", "supersede", "conflict", "pending_review", "discard"}
MEMORY_RELATION_TYPES = {"supersedes", "superseded_by", "conflicts_with", "supports", "summarized_from", "derived_from"}
MEMORY_EXTRACTION_STATUSES = {"pending", "processing", "completed", "empty", "partial", "failed"}
MEMORY_CONSOLIDATION_STATUSES = {"running", "completed", "failed"}


def utc_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def candidate_id_for(note_id: str, memory_type: str, content: str) -> str:
    """Return a stable candidate id so retries remain auditable and idempotent."""
    key = f"{note_id}\x1f{memory_type}\x1f{normalize_content(content)}"
    return f"cand_{uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:16]}"


def normalize_content(text: str) -> str:
    value = str(text or "").casefold()
    value = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)
    for token in ("用户", "我现在", "我最近", "我", "本人", "目前", "现在", "最近"):
        value = value.replace(token, "")
    return value


@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: str
    content: str
    importance: float
    confidence: float
    entities: list[str] = field(default_factory=list)
    should_store: bool = True
    task_status: str | None = None
    reason: str | None = None
    candidate_id: str = field(default_factory=lambda: new_id("cand"))
    note_id: str | None = None
    space_id: str | None = None
    subject: str | None = None
    predicate: str | None = None
    object_value: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    evidence_span: str | None = None
    extraction_reason: str | None = None

    def __post_init__(self) -> None:
        if self.memory_type not in MEMORY_TYPES:
            raise ValueError(f"invalid memory_type: {self.memory_type}")
        if self.task_status is not None and self.task_status not in TASK_STATUSES:
            raise ValueError(f"invalid task_status: {self.task_status}")

    @property
    def normalized_content(self) -> str:
        return normalize_content(self.content)

    @property
    def effective_reason(self) -> str | None:
        return self.extraction_reason or self.reason


@dataclass(frozen=True, kw_only=True)
class MemoryDecision:
    candidate_id: str
    relation: str
    target_memory_ids: list[str]
    confidence: float
    reason: str
    evidence: list[str]
    recommended_action: str
    decision_id: str = field(default_factory=lambda: new_id("decision"))

    def __post_init__(self) -> None:
        if self.relation not in DECISION_RELATIONS:
            raise ValueError(f"invalid decision relation: {self.relation}")
        if self.recommended_action not in DECISION_ACTIONS:
            raise ValueError(f"invalid decision action: {self.recommended_action}")
        if not 0 <= float(self.confidence) <= 1:
            raise ValueError("decision confidence must be between 0 and 1")


@dataclass(frozen=True)
class MemorySource:
    memory_id: str
    note_id: str
    relation: str
    created_at: str


@dataclass(frozen=True)
class MemoryVersion:
    id: str
    memory_id: str
    version: int
    content: str
    status: str
    reason: str | None
    source_note_id: str | None
    created_at: str
    task_status: str | None = None
    confidence: float | None = None
    importance: float | None = None
    valid_from: str | None = None
    valid_until: str | None = None


@dataclass(frozen=True)
class MemoryRelation:
    id: str
    space_id: str
    source_memory_id: str
    target_memory_id: str
    relation: str
    decision_id: str | None
    created_at: str


@dataclass(frozen=True)
class MemoryExtractionState:
    note_id: str
    space_id: str
    status: str
    candidate_count: int
    processed_count: int
    attempt_count: int
    last_error: str | None
    started_at: str | None
    completed_at: str | None
    updated_at: str


@dataclass(frozen=True)
class ConsolidationRun:
    id: str
    space_id: str
    cadence: str
    period_key: str
    status: str
    started_at: str
    completed_at: str | None
    error: str | None
    result_json: str | None


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    space_id: str
    memory_type: str
    content: str
    normalized_content: str
    importance: float
    confidence: float
    status: str
    valid_from: str | None
    valid_until: str | None
    created_at: str
    updated_at: str
    last_accessed_at: str | None
    access_count: int
    current_version: int
    task_status: str | None = None
    sources: list[MemorySource] = field(default_factory=list)
    versions: list[MemoryVersion] = field(default_factory=list)
    subject: str | None = None
    predicate: str | None = None
    object_value: str | None = None
    last_confirmed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "memory_type": self.memory_type,
            "content": self.content,
            "normalized_content": self.normalized_content,
            "importance": self.importance,
            "confidence": self.confidence,
            "status": self.status,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_accessed_at": self.last_accessed_at,
            "access_count": self.access_count,
            "current_version": self.current_version,
            "task_status": self.task_status,
            "subject": self.subject,
            "predicate": self.predicate,
            "object_value": self.object_value,
            "last_confirmed_at": self.last_confirmed_at,
            "sources": [source.__dict__ for source in self.sources],
            "versions": [version.__dict__ for version in self.versions],
        }
