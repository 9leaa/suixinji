"""Clause-level splitting for memory extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Clause:
    index: int
    text: str
    start: int
    end: int


_BOUNDARY_RE = re.compile(r"[。！？!?；;]|(?:，|,)(?=(?:我|用户|本人|下周|明天|今天|昨天|刚才|需要|记得|还要|并且|而且|但是|不过|同时))")
_LEADING_CONNECTOR_RE = re.compile(r"^(?:并且|而且|但是|不过|同时|然后|接着|另外|还|以及|，|,)\s*")


def split_clauses(text: str, *, max_clauses: int = 8) -> list[Clause]:
    raw = str(text or "")
    clauses: list[Clause] = []
    start = 0
    for match in _BOUNDARY_RE.finditer(raw):
        end = match.start()
        _append_clause(clauses, raw, start, end, max_clauses)
        start = match.end()
        if len(clauses) >= max_clauses:
            break
    if len(clauses) < max_clauses:
        _append_clause(clauses, raw, start, len(raw), max_clauses)
    return clauses


def _append_clause(clauses: list[Clause], raw: str, start: int, end: int, max_clauses: int) -> None:
    if len(clauses) >= max_clauses:
        return
    segment = raw[start:end].strip()
    segment = _LEADING_CONNECTOR_RE.sub("", segment).strip()
    if not segment:
        return
    if len(segment) <= 2:
        return
    clauses.append(Clause(index=len(clauses), text=segment, start=start, end=end))
