"""Explicit mappings between production and Layer 2 evaluation enums."""

from __future__ import annotations

EVAL_RELATIONS = ("new", "same", "merge", "update", "supersede", "conflict")
EVAL_ACTIONS = ("insert", "add_source", "update", "pending_review")


def normalize_relation(relation: str | None, action: str | None = None) -> str | None:
    value = str(relation or "")
    if value == "orphan_completion":
        return "conflict" if action == "pending_review" else "new"
    if value == "ambiguous_match":
        return "conflict"
    if value == "update_task":
        return "update"
    return value or None


def normalize_action(action: str | None) -> str | None:
    value = str(action or "")
    if value in {"update_task", "merge", "supersede"}:
        return "update" if value == "merge" else ("insert" if value == "supersede" else "update")
    if value == "conflict":
        return "pending_review"
    return value or None


PRODUCTION_RELATION_TO_EVAL = {
    "new": "new",
    "same": "same",
    "merge": "merge",
    "update_task": "update",
    "supersede": "supersede",
    "conflict": "conflict",
    "orphan_completion": "context_dependent",
    "ambiguous_match": "conflict",
}

PRODUCTION_ACTION_TO_EVAL = {
    "insert": "insert",
    "add_source": "add_source",
    "update_task": "update",
    "merge": "update",
    "supersede": "insert",
    "pending_review": "pending_review",
    "conflict": "pending_review",
}
