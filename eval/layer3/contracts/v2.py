"""Validation helpers for the immutable Layer 3 v2 evaluation contract."""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "suixinji.layer3.retrieval_answer.v2"
ANSWER_TYPES = frozenset({
    "answered",
    "no_answer",
    "qualified_history_only",
    "conflict",
    "clarification",
    "restricted",
    "system_error",
})
EVIDENCE_MODES = frozenset({"current", "history", "mixed", "none"})
NON_FACT_ANSWER_TYPES = frozenset({"no_answer", "conflict", "clarification", "restricted", "system_error"})


class ContractValidationError(ValueError):
    """Raised when an evaluation case does not conform to Layer 3 v2."""


def _error(case: dict[str, Any], message: str) -> ContractValidationError:
    return ContractValidationError(f"{case.get('case_id', '<unknown>')}: {message}")


def validate_case(case: dict[str, Any]) -> None:
    """Validate only v2 fields; it never mutates the supplied case."""
    if case.get("schema_version") != SCHEMA_VERSION:
        raise _error(case, f"schema_version must be {SCHEMA_VERSION}")
    expected = case.get("expected")
    if not isinstance(expected, dict):
        raise _error(case, "expected must be an object")
    answer_type = expected.get("answer_type")
    if answer_type not in ANSWER_TYPES:
        raise _error(case, f"unsupported answer_type: {answer_type!r}")
    mode = expected.get("evidence_mode")
    if mode not in EVIDENCE_MODES:
        raise _error(case, f"unsupported evidence_mode: {mode!r}")
    reason = expected.get("reason_code")
    if not isinstance(reason, str) or not reason:
        raise _error(case, "reason_code is required")
    if answer_type == "answered" and mode == "none":
        raise _error(case, "answered cannot use evidence_mode=none")
    if answer_type == "qualified_history_only" and mode != "history":
        raise _error(case, "qualified_history_only must use evidence_mode=history")
    if answer_type in {"no_answer", "restricted", "clarification"} and mode != "none":
        raise _error(case, f"{answer_type} must use evidence_mode=none")
    if answer_type == "conflict" and mode not in {"current", "mixed"}:
        raise _error(case, "conflict must use current or mixed evidence")
    if answer_type in NON_FACT_ANSWER_TYPES and expected.get("expected_claims"):
        raise _error(case, f"{answer_type} must not require factual expected_claims")
    groups = expected.get("expected_claim_groups") or []
    if not isinstance(groups, list):
        raise _error(case, "expected_claim_groups must be a list")
    for group in groups:
        if not isinstance(group, dict) or group.get("group_type") != "timeline":
            raise _error(case, "claim groups must be timeline objects")
        members = group.get("ordered_members")
        if not isinstance(members, list) or len(members) < 2:
            raise _error(case, "timeline group requires at least two ordered members")
        if not all(isinstance(member, dict) and member.get("version_ref") for member in members):
            raise _error(case, "every timeline member requires version_ref")
        if not isinstance(group.get("summary_claim"), dict):
            raise _error(case, "timeline group requires summary_claim")


def validate_cases(cases: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for case in cases:
        validate_case(case)
        case_id = str(case.get("case_id") or "")
        if not case_id or case_id in seen:
            raise ContractValidationError(f"duplicate or empty case_id: {case_id!r}")
        seen.add(case_id)
