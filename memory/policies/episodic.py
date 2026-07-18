"""Episodic retention policy."""

from __future__ import annotations

from datetime import datetime


def age_days(created_at: str, *, now: datetime | None = None) -> float:
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return 0.0
    current = now or datetime.now().astimezone()
    if created.tzinfo is None:
        created = created.replace(tzinfo=current.tzinfo)
    return max(0.0, (current - created).total_seconds() / 86400)


def recency_weight(created_at: str, *, now: datetime | None = None) -> float:
    age = age_days(created_at, now=now)
    return max(0.15, 1.0 - min(age, 365.0) / 365.0)
