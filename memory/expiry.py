"""Expire due memories without deleting their audit history."""

from __future__ import annotations

from typing import Any

from memory.repository import expire_due_memories


def run_expiry_once(*, space_id: str | None = None, limit: int = 500) -> dict[str, Any]:
    expired = expire_due_memories(space_id, limit=limit)
    return {"space_id": space_id, "expired_count": expired, "status": "completed"}
