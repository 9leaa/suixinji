"""Expire due memories without deleting their audit history."""

from __future__ import annotations

from typing import Any

from memory.repository import expire_due_memories


def run_expiry_once(*, space_id: str | None = None, limit: int = 500) -> dict[str, Any]:
    """负责“运行expiryonce”。

    该函数是 `memory.expiry` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    expired = expire_due_memories(space_id, limit=limit)
    return {"space_id": space_id, "expired_count": expired, "status": "completed"}
