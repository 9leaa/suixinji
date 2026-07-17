"""Semantic fact replacement policy."""

from __future__ import annotations


CHANGE_MARKERS = ("现在", "改为", "搬到", "转为", "不再", "短期", "只学", "重点")


def explicitly_replaces(new_content: str, *, predicate: str | None = None) -> bool:
    if predicate == "location" and any(marker in new_content for marker in ("搬到", "住在")):
        return True
    return any(marker in new_content for marker in CHANGE_MARKERS)
