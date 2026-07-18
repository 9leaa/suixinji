"""PostgreSQL automatic summary subscription repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from core.settings import SUMMARY_DEFAULT_TIME
from infrastructure.database import session_scope
from infrastructure.schema import SummarySubscriptionRow
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space


def _subscription(row: SummarySubscriptionRow):
    from summary.subscription import SummarySubscription
    return SummarySubscription(
        space_id=row.space_id,
        chat_id=row.chat_id,
        enabled=row.enabled,
        time=row.time,
        range_key=row.range_key,
        last_sent_date=row.last_sent_date,
    )


def get_summary_subscription(space_id: str):
    with session_scope() as session:
        row = session.get(SummarySubscriptionRow, space_id)
        return _subscription(row) if row is not None else None


def list_enabled_summary_subscriptions() -> list:
    with session_scope() as session:
        rows = session.execute(
            select(SummarySubscriptionRow).where(SummarySubscriptionRow.enabled.is_(True)).order_by(SummarySubscriptionRow.space_id)
        ).scalars()
        return [_subscription(row) for row in rows]


def _upsert(space_id: str, chat_id: str, **updates):
    tenant_id = DEFAULT_TENANT_ID
    with session_scope() as session:
        ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        old = session.get(SummarySubscriptionRow, space_id)
        values = {
            "tenant_id": tenant_id,
            "chat_id": chat_id,
            "enabled": bool(updates.get("enabled", old.enabled if old else True)),
            "time": str(updates.get("time", old.time if old else SUMMARY_DEFAULT_TIME)),
            "range_key": str(updates.get("range_key", old.range_key if old else "today")),
            "last_sent_date": updates.get("last_sent_date", old.last_sent_date if old else None),
        }
        session.execute(
            insert(SummarySubscriptionRow)
            .values(space_id=space_id, **values)
            .on_conflict_do_update(index_elements=[SummarySubscriptionRow.space_id], set_=values)
        )
    return get_summary_subscription(space_id)


def enable_summary_subscription(space_id: str, chat_id: str):
    return _upsert(space_id, chat_id, enabled=True)


def disable_summary_subscription(space_id: str):
    old = get_summary_subscription(space_id)
    if old is None:
        return None
    return _upsert(space_id, old.chat_id, enabled=False)


def update_summary_time(space_id: str, chat_id: str, time_value: str):
    from summary.subscription import parse_summary_time
    parsed = parse_summary_time(time_value)
    if parsed is None:
        raise ValueError("time must be HH:MM, for example 22:00")
    return _upsert(space_id, chat_id, time=parsed)


def mark_summary_sent(space_id: str, sent_date: str) -> None:
    old = get_summary_subscription(space_id)
    if old is not None:
        _upsert(space_id, old.chat_id, last_sent_date=sent_date)
