"""Lifecycle helpers for Memory V2."""

from __future__ import annotations

from memory.repository import correct_memory, soft_delete_memory, update_memory


def forget(memory_id: str) -> bool:
    return soft_delete_memory(memory_id) is not None


def correct(memory_id: str, content: str) -> bool:
    return correct_memory(memory_id, content) is not None


def expire(memory_id: str, reason: str = "expired") -> bool:
    return update_memory(memory_id, status="expired", reason=reason) is not None

