"""Structured answer contract used by the query API and evaluators."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

ANSWER_TYPES = {
    "answered", "no_answer", "qualified_history_only", "conflict",
    "clarification", "restricted", "system_error",
}

EVIDENCE_KINDS = {
    "memory", "version", "source", "tool_result", "pending_review",
    "access_denied",
}

EVIDENCE_ROLES = {
    "current", "history", "stale_history", "conflict", "candidate",
    "restricted", "access_denied",
}


@dataclass
class RetrievalEvidence:
    """One production evidence item. logical_ref is evaluator-only metadata."""

    kind: str
    id: str
    memory_id: str | None = None
    version_id: str | None = None
    source_ids: list[str] = field(default_factory=list)
    memory_type: str | None = None
    status: str | None = None
    task_status: str | None = None
    score: float | None = None
    rank: int | None = None
    channel: str | None = None
    tool: str | None = None
    selected: bool = False
    role: str = "candidate"
    logical_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            self.kind = "tool_result"
        if self.role not in EVIDENCE_ROLES:
            self.role = "candidate"
        self.id = str(self.id or "")
        if self.memory_id is not None:
            self.memory_id = str(self.memory_id)
        if self.version_id is not None:
            self.version_id = str(self.version_id)
        self.source_ids = [str(item) for item in self.source_ids if str(item or "")]
        if self.logical_ref is not None:
            self.logical_ref = str(self.logical_ref)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceBundle:
    items: list[RetrievalEvidence] = field(default_factory=list)
    selected_context_refs: list[str] = field(default_factory=list)
    selected_tool_refs: list[str] = field(default_factory=list)
    executed_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "EvidenceBundle | None":
        if not isinstance(value, dict):
            return None
        items = [
            item if isinstance(item, RetrievalEvidence) else RetrievalEvidence(**item)
            for item in value.get("items", [])
            if isinstance(item, (dict, RetrievalEvidence))
        ]
        return cls(
            items=items,
            selected_context_refs=[str(item) for item in value.get("selected_context_refs", []) if str(item or "")],
            selected_tool_refs=[str(item) for item in value.get("selected_tool_refs", []) if str(item or "")],
            executed_tools=[str(item) for item in value.get("executed_tools", []) if str(item or "")],
        )

    def __post_init__(self) -> None:
        self.items = [
            item if isinstance(item, RetrievalEvidence) else RetrievalEvidence(**item)
            for item in self.items
            if isinstance(item, (dict, RetrievalEvidence))
        ]
        if not self.selected_context_refs:
            selected: list[str] = []
            for item in self.items:
                if not item.selected:
                    continue
                ref = item.version_id or item.memory_id or item.id
                if ref and ref not in selected:
                    selected.append(ref)
            self.selected_context_refs = selected
        if not self.selected_tool_refs:
            refs: list[str] = []
            for item in self.items:
                if item.selected and item.tool and item.tool not in refs:
                    refs.append(item.tool)
            self.selected_tool_refs = refs
        if not self.executed_tools:
            tools: list[str] = []
            for item in self.items:
                if item.tool and item.tool not in tools:
                    tools.append(item.tool)
            self.executed_tools = tools

    @property
    def selected_memory_ids(self) -> list[str]:
        values: list[str] = []
        for item in self.items:
            if item.selected and item.memory_id and item.memory_id not in values:
                values.append(item.memory_id)
        return values

    @property
    def selected_version_ids(self) -> list[str]:
        values: list[str] = []
        for item in self.items:
            if item.selected and item.version_id and item.version_id not in values:
                values.append(item.version_id)
        return values

    @property
    def selected_source_ids(self) -> list[str]:
        values: list[str] = []
        for item in self.items:
            if not item.selected:
                continue
            for source_id in item.source_ids:
                if source_id not in values:
                    values.append(source_id)
        return values

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SupportedClaim:
    text: str
    claim_type: str = "fact"
    memory_ids: list[str] = field(default_factory=list)
    version_ids: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass
class AnswerDecision:
    answer_type: str
    reason_code: str
    confidence: float = 1.0
    conflict_ids: list[str] = field(default_factory=list)
    clarification_options: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.answer_type not in ANSWER_TYPES:
            self.answer_type = "system_error"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass
class AnswerResult:
    answer_type: str
    answer: str
    reason_code: str = "evidence_supported"
    claims: list[SupportedClaim] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    selected_memory_ids: list[str] = field(default_factory=list)
    selected_version_ids: list[str] = field(default_factory=list)
    selected_source_ids: list[str] = field(default_factory=list)
    selected_context_refs: list[str] = field(default_factory=list)
    selected_tool_refs: list[str] = field(default_factory=list)
    executed_tools: list[str] = field(default_factory=list)
    evidence_bundle: EvidenceBundle | None = None
    evidence_mode: str | None = None
    retryable: bool = False
    decision: AnswerDecision | None = None

    def __post_init__(self) -> None:
        if self.answer_type not in ANSWER_TYPES:
            self.answer_type = "system_error"
        if isinstance(self.evidence_bundle, dict):
            self.evidence_bundle = EvidenceBundle.from_dict(self.evidence_bundle)
        if self.evidence_bundle is not None:
            if not self.selected_context_refs:
                self.selected_context_refs = list(self.evidence_bundle.selected_context_refs)
            if not self.selected_tool_refs:
                self.selected_tool_refs = list(self.evidence_bundle.selected_tool_refs)
            if not self.executed_tools:
                self.executed_tools = list(self.evidence_bundle.executed_tools)
            if not self.selected_memory_ids:
                self.selected_memory_ids = list(self.evidence_bundle.selected_memory_ids)
            if not self.selected_version_ids:
                self.selected_version_ids = list(self.evidence_bundle.selected_version_ids)
            if not self.selected_source_ids:
                self.selected_source_ids = list(self.evidence_bundle.selected_source_ids)

    @property
    def availability_status(self) -> str:
        return "available" if self.answer_type in {"answered", "qualified_history_only", "conflict"} else self.answer_type

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["availability_status"] = self.availability_status
        return payload
