"""Semantic fact replacement policy."""

from __future__ import annotations


CHANGE_MARKERS = ("现在", "改为", "搬到", "转为", "不再", "短期", "只学", "重点")


def explicitly_replaces(new_content: str, *, predicate: str | None = None) -> bool:
    """负责“explicitlyreplaces”。

    该函数是 `memory.policies.semantic` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    if predicate == "location" and any(marker in new_content for marker in ("搬到", "现在住在", "已经住在", "改住")):
        return True
    return any(marker in new_content for marker in CHANGE_MARKERS)
