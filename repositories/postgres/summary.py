"""文件作用：Summary 数据访问。

项目关系：本文件依赖 `core.settings`、`infrastructure.database`、`infrastructure.schema`、`repositories.postgres.common` 等 5 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from core.settings import SUMMARY_DEFAULT_TIME
from infrastructure.database import session_scope
from infrastructure.schema import SummarySubscriptionRow
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space


def _subscription(row: SummarySubscriptionRow):
    """函数功能：`_subscription` 负责处理 subscription，服务于本文件职责：Summary 数据访问。
    传参：
        row: row 参数，由调用方传入，类型为 `SummarySubscriptionRow`。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
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
    """函数功能：`get_summary_subscription` 负责获取 summary subscription，服务于本文件职责：Summary 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    with session_scope() as session:
        row = session.get(SummarySubscriptionRow, space_id)
        return _subscription(row) if row is not None else None


def list_enabled_summary_subscriptions() -> list:
    """函数功能：`list_enabled_summary_subscriptions` 负责列出 enabled summary subscriptions，服务于本文件职责：Summary 数据访问。
    传参：
        无。
    返回结果说明：
        返回 `list`，表示按条件筛选、构造或查询得到的列表。
    """
    with session_scope() as session:
        rows = session.execute(
            select(SummarySubscriptionRow).where(SummarySubscriptionRow.enabled.is_(True)).order_by(SummarySubscriptionRow.space_id)
        ).scalars()
        return [_subscription(row) for row in rows]


def _upsert(space_id: str, chat_id: str, **updates):
    """函数功能：`_upsert` 负责插入或更新，服务于本文件职责：Summary 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        chat_id: chat id 参数，由调用方传入，类型为 `str`。
        **updates: updates 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
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
    """函数功能：`enable_summary_subscription` 负责处理 enable summary subscription，服务于本文件职责：Summary 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        chat_id: chat id 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    return _upsert(space_id, chat_id, enabled=True)


def disable_summary_subscription(space_id: str):
    """函数功能：`disable_summary_subscription` 负责处理 disable summary subscription，服务于本文件职责：Summary 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    old = get_summary_subscription(space_id)
    if old is None:
        return None
    return _upsert(space_id, old.chat_id, enabled=False)


def update_summary_time(space_id: str, chat_id: str, time_value: str):
    """函数功能：`update_summary_time` 负责更新 summary time，服务于本文件职责：Summary 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        chat_id: chat id 参数，由调用方传入，类型为 `str`。
        time_value: time value 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    from summary.subscription import parse_summary_time
    parsed = parse_summary_time(time_value)
    if parsed is None:
        raise ValueError("time must be HH:MM, for example 22:00")
    return _upsert(space_id, chat_id, time=parsed)


def mark_summary_sent(space_id: str, sent_date: str) -> None:
    """函数功能：`mark_summary_sent` 负责标记 summary sent，服务于本文件职责：Summary 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        sent_date: sent date 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    old = get_summary_subscription(space_id)
    if old is not None:
        _upsert(space_id, old.chat_id, last_sent_date=sent_date)
