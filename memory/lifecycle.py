"""Lifecycle helpers for Memory V2."""

from __future__ import annotations

from memory.repository import correct_memory, soft_delete_memory, update_memory


def forget(memory_id: str) -> bool:
    """负责“forget”。

    该函数是 `memory.lifecycle` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return soft_delete_memory(memory_id) is not None


def correct(memory_id: str, content: str) -> bool:
    """负责“correct”。

    该函数是 `memory.lifecycle` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return correct_memory(memory_id, content) is not None


def expire(memory_id: str, reason: str = "expired") -> bool:
    """负责“expire”。

    该函数是 `memory.lifecycle` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return update_memory(memory_id, status="expired", reason=reason) is not None

