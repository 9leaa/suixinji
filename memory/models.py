"""Shared models and constants for Memory V2."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MEMORY_TYPES = {"episodic", "semantic", "preference", "task"}
MEMORY_STATUSES = {"active", "superseded", "conflicted", "deleted", "expired"}
TASK_STATUSES = {"todo", "in_progress", "blocked", "done", "cancelled"}
SOURCE_RELATIONS = {"created_from", "supported_by", "updated_by", "contradicted_by"}
MEMORY_EXTRACTION_STATUSES = {"pending", "processing", "completed", "empty", "partial", "failed"}
MEMORY_CONSOLIDATION_STATUSES = {"running", "completed", "failed"}


def utc_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


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

    def __post_init__(self) -> None:
        if self.memory_type not in MEMORY_TYPES:
            raise ValueError(f"invalid memory_type: {self.memory_type}")
        if self.task_status is not None and self.task_status not in TASK_STATUSES:
            raise ValueError(f"invalid task_status: {self.task_status}")

    @property
    def normalized_content(self) -> str:
        return normalize_content(self.content)


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
            "sources": [source.__dict__ for source in self.sources],
            "versions": [version.__dict__ for version in self.versions],
        }
