"""Access predicates shared by retrieval and answer orchestration.

The query layer must never use a post-hoc text filter as its ACL.  This small
module keeps the policy explicit and makes the same predicate usable by every
repository channel (exact, structured, FTS, trigram and vector).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AccessContext:
    requester: str | None = None
    owner_id: str | None = None
    allow_sensitive: bool = False
    allow_restricted: bool = False

    @classmethod
    def from_value(cls, value: Any = None, *, requester: str | None = None) -> "AccessContext":
        if isinstance(value, cls):
            return value
        data = value if isinstance(value, dict) else {}
        return cls(
            requester=str(data.get("requester") or requester or "") or None,
            owner_id=str(data.get("owner_id") or data.get("owner") or "owner") or None,
            allow_sensitive=bool(data.get("allow_sensitive", False)),
            allow_restricted=bool(data.get("allow_restricted", False)),
        )


def access_decision(scope: dict[str, Any] | None, context: AccessContext | dict[str, Any] | None = None) -> tuple[bool, str]:
    scope = scope if isinstance(scope, dict) else {}
    # Legacy memories have no scope metadata; they remain queryable for the
    # owner while newly seeded restricted records carry an explicit policy.
    if not scope:
        return True, "legacy_unscoped_allowed"
    ctx = AccessContext.from_value(context)
    sensitivity = str(scope.get("sensitivity") or "normal").casefold()
    access_scope = str(scope.get("access_scope") or scope.get("scope") or "owner").casefold()
    owner = str(scope.get("owner_id") or ctx.owner_id or "owner")
    requester = str(ctx.requester or "owner")
    if sensitivity in {"sensitive", "secret", "restricted"} and not ctx.allow_sensitive:
        return False, "sensitive_denied"
    if access_scope in {"restricted", "private", "owner", "owner_only"}:
        if access_scope in {"restricted", "private"} and not ctx.allow_restricted:
            return False, "restricted_denied"
        if access_scope in {"owner", "owner_only"} and requester != owner and not ctx.allow_restricted:
            return False, "owner_only_denied"
    return True, "allowed"


def memory_access_allowed(memory: Any, context: AccessContext | dict[str, Any] | None = None) -> bool:
    scope = getattr(memory, "scope", None)
    if scope is None:
        scope = getattr(memory, "scope_json", None)
    if scope is None and isinstance(memory, dict):
        scope = memory.get("scope") or memory.get("scope_json")
    return access_decision(scope, context)[0]
