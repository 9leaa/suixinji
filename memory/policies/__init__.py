"""Lifecycle policies for each long-term memory type."""

from __future__ import annotations

from memory.models import normalize_content


def merge_content(memory_type: str, old_content: str, new_content: str) -> str:
    """Produce a deterministic, loss-minimising merge without delegating writes to a model."""
    old_norm = normalize_content(old_content)
    new_norm = normalize_content(new_content)
    if not old_norm:
        return new_content
    if old_norm in new_norm:
        return new_content
    if new_norm in old_norm:
        return old_content
    separator = "；"
    return f"{old_content.rstrip('。；; ')}{separator}{new_content.lstrip('用户').rstrip('。；; ')}"
