"""文件作用：Delivery 数据访问。

项目关系：本文件依赖 `core.settings`、`infrastructure.database`、`infrastructure.schema`、`repositories.postgres.common` 等 5 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from core.settings import DELIVERY_MAX_ATTEMPTS, DELIVERY_RESERVATION_TTL_SECONDS
from infrastructure.database import session_scope
from infrastructure.schema import Delivery, DeliveryAttempt
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space

DELIVERY_RESERVED = "reserved"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"
DELIVERY_UNKNOWN = "unknown"


def _record(row: Delivery):
    """函数功能：`_record` 负责记录，服务于本文件职责：Delivery 数据访问。
    传参：
        row: row 参数，由调用方传入，类型为 `Delivery`。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    from runtime.delivery_store import DeliveryRecord
    return DeliveryRecord(
        delivery_key=row.delivery_key,
        delivery_type=row.delivery_type,
        space_id=row.space_id,
        message_id=row.message_id,
        status=row.status,
        created_at=row.created_at.isoformat() if isinstance(row.created_at, datetime) else str(row.created_at),
        updated_at=row.updated_at.isoformat() if isinstance(row.updated_at, datetime) else str(row.updated_at),
        reserved_at=row.reserved_at.isoformat() if isinstance(row.reserved_at, datetime) else row.reserved_at,
        lease_expires_at=row.lease_expires_at.isoformat() if isinstance(row.lease_expires_at, datetime) else row.lease_expires_at,
        attempt_count=row.attempt_count,
        error=row.error,
    )


def _future_dt(seconds: int) -> datetime:
    """函数功能：`_future_dt` 负责处理 future dt，服务于本文件职责：Delivery 数据访问。
    传参：
        seconds: seconds 参数，由调用方传入，类型为 `int`。
    返回结果说明：
        返回 `datetime` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return datetime.now().astimezone() + timedelta(seconds=seconds)


def _parse_iso(value: str | datetime) -> datetime:
    """函数功能：`_parse_iso` 负责解析 iso，服务于本文件职责：Delivery 数据访问。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | datetime`。
    返回结果说明：
        返回 `datetime` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def is_reservation_expired(record, now: datetime | None = None) -> bool:
    """函数功能：`is_reservation_expired` 负责判断是否为 reservation expired，服务于本文件职责：Delivery 数据访问。
    传参：
        record: 待处理或持久化的记录对象。
        now: now 参数，由调用方传入，类型为 `datetime | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if record.status != DELIVERY_RESERVED:
        return False
    if not record.lease_expires_at:
        return True
    return _parse_iso(record.lease_expires_at) <= (now or datetime.now().astimezone())


def reserve_delivery(
    delivery_key: str,
    *,
    delivery_type: str,
    space_id: str,
    message_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
):
    """函数功能：`reserve_delivery` 负责预约 delivery，服务于本文件职责：Delivery 数据访问。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
        delivery_type: delivery type 参数，由调用方传入，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`，默认值为 `DEFAULT_TENANT_ID`。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    with session_scope() as session:
        space_id = ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:delivery_key))"), {"delivery_key": delivery_key})
        row = session.execute(
            select(Delivery).where(Delivery.delivery_key == delivery_key).with_for_update()
        ).scalar_one_or_none()
        if row is not None:
            current = _record(row)
            if current.status in {DELIVERY_SENT, DELIVERY_UNKNOWN}:
                return None
            if current.status == DELIVERY_RESERVED and not is_reservation_expired(current):
                return None
            if current.attempt_count >= DELIVERY_MAX_ATTEMPTS:
                return None

        now = datetime.now().astimezone()
        attempt_count = (row.attempt_count if row is not None else 0) + 1
        created_at = row.created_at if row is not None else now
        values = {
            "delivery_type": delivery_type,
            "tenant_id": tenant_id,
            "space_id": space_id,
            "message_id": message_id,
            "status": DELIVERY_RESERVED,
            "created_at": created_at,
            "updated_at": now,
            "reserved_at": now,
            "lease_expires_at": _future_dt(DELIVERY_RESERVATION_TTL_SECONDS),
            "attempt_count": attempt_count,
            "error": None,
        }
        if row is None:
            row = Delivery(delivery_key=delivery_key, **values)
            session.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        session.execute(
            insert(DeliveryAttempt)
            .values(
                delivery_key=delivery_key,
                attempt_no=attempt_count,
                status=DELIVERY_RESERVED,
                started_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_delivery_attempt_no")
        )
        session.flush()
        return _record(row)


def _update_status(delivery_key: str, status: str, error: str | None) -> None:
    """函数功能：`_update_status` 负责更新 status，服务于本文件职责：Delivery 数据访问。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str | None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    now = datetime.now().astimezone()
    with session_scope() as session:
        row = session.execute(select(Delivery).where(Delivery.delivery_key == delivery_key).with_for_update()).scalar_one_or_none()
        if row is None:
            return
        row.status = status
        row.updated_at = now
        row.error = error
        session.execute(
            update(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_key == delivery_key, DeliveryAttempt.attempt_no == row.attempt_count)
            .values(status=status, finished_at=now, error=error)
        )


def mark_sent(delivery_key: str) -> None:
    """函数功能：`mark_sent` 负责标记 sent，服务于本文件职责：Delivery 数据访问。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    _update_status(delivery_key, DELIVERY_SENT, None)


def mark_failed(delivery_key: str, error: str) -> None:
    """函数功能：`mark_failed` 负责标记 failed，服务于本文件职责：Delivery 数据访问。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    _update_status(delivery_key, DELIVERY_FAILED, error)


def mark_unknown(delivery_key: str, error: str) -> None:
    """函数功能：`mark_unknown` 负责标记 unknown，服务于本文件职责：Delivery 数据访问。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    _update_status(delivery_key, DELIVERY_UNKNOWN, error)


def get_delivery(delivery_key: str):
    """函数功能：`get_delivery` 负责获取 delivery，服务于本文件职责：Delivery 数据访问。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    with session_scope() as session:
        row = session.get(Delivery, delivery_key)
        return _record(row) if row is not None else None


def recover_stale_reserved_deliveries() -> int:
    """函数功能：`recover_stale_reserved_deliveries` 负责处理 recover stale reserved deliveries，服务于本文件职责：Delivery 数据访问。
    传参：
        无。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    now = datetime.now().astimezone()
    with session_scope() as session:
        rows = list(session.execute(select(Delivery).where(Delivery.status == DELIVERY_RESERVED).with_for_update()).scalars())
        stale = [row for row in rows if not row.lease_expires_at or _parse_iso(row.lease_expires_at) <= now]
        finished_at = datetime.now().astimezone()
        for row in stale:
            row.status = DELIVERY_FAILED
            row.updated_at = finished_at
            row.error = "reservation lease expired"
            session.execute(
                update(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_key == row.delivery_key, DeliveryAttempt.attempt_no == row.attempt_count)
                .values(status=DELIVERY_FAILED, finished_at=finished_at, error=row.error)
            )
        return len(stale)
