"""文件作用：总结发送对账。

项目关系：本文件依赖 `core.observability`、`core.settings`、`runtime.delivery_store`、`summary.subscription`；被 `summary.scheduler`。
"""



from __future__ import annotations

from core.observability import log_event
from core.settings import DELIVERY_MAX_ATTEMPTS
from runtime.delivery_store import (
    DELIVERY_FAILED,
    DELIVERY_RESERVED,
    DELIVERY_SENT,
    DELIVERY_UNKNOWN,
    auto_summary_key,
    get_delivery,
    is_reservation_expired,
    mark_failed,
)
from summary.subscription import get_summary_subscription, mark_summary_sent


def reconcile_auto_summary_delivery(space_id: str, range_key: str, sent_date: str) -> bool:
    """函数功能：`reconcile_auto_summary_delivery` 负责对账 auto summary delivery，服务于本文件职责：总结发送对账。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        range_key: range key 参数，由调用方传入，类型为 `str`。
        sent_date: sent date 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    delivery_key = auto_summary_key(space_id, range_key, sent_date)
    delivery = get_delivery(delivery_key)
    if delivery is None:
        return False

    if delivery.status == DELIVERY_SENT:
        sub = get_summary_subscription(space_id)
        if sub is None or sub.last_sent_date != sent_date:
            mark_summary_sent(space_id, sent_date)
            log_event(
                "summary.auto.reconcile",
                status="success",
                space_id=space_id,
                extra={"range_key": range_key, "sent_date": sent_date, "delivery_key": delivery_key},
            )
        return True

    if delivery.status == DELIVERY_UNKNOWN:
        log_event(
            "summary.auto.reconcile",
            level="warning",
            status="skipped",
            space_id=space_id,
            error="auto summary delivery status is unknown",
            extra={"range_key": range_key, "sent_date": sent_date, "delivery_key": delivery_key},
        )
        return True

    if delivery.status == DELIVERY_RESERVED:
        if is_reservation_expired(delivery):
            mark_failed(delivery_key, "reservation lease expired")
            log_event(
                "summary.auto.reconcile",
                level="warning",
                status="failed",
                space_id=space_id,
                error="auto summary delivery reservation expired",
                extra={"range_key": range_key, "sent_date": sent_date, "delivery_key": delivery_key},
            )
            return False

        log_event(
            "summary.auto.reconcile",
            status="skipped",
            space_id=space_id,
            extra={"range_key": range_key, "sent_date": sent_date, "delivery_key": delivery_key, "reason": "reserved"},
        )
        return True

    if delivery.status == DELIVERY_FAILED:
        if delivery.attempt_count >= DELIVERY_MAX_ATTEMPTS:
            log_event(
                "summary.auto.reconcile",
                level="warning",
                status="skipped",
                space_id=space_id,
                error="auto summary delivery attempts exhausted",
                extra={
                    "range_key": range_key,
                    "sent_date": sent_date,
                    "delivery_key": delivery_key,
                    "attempt_count": delivery.attempt_count,
                },
            )
            return True
        return False

    return False
