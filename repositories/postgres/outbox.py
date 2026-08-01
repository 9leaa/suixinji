"""文件作用：Outbox 数据访问。

项目关系：本文件依赖 `core.observability`、`core.settings`、`infrastructure.database`、`infrastructure.schema`；被 `apps.outbox_relay`、`tests.test_streams_outbox`。
"""



from __future__ import annotations

import socket
import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import or_, select, update

from core.observability import log_event
from core.settings import OUTBOX_LEASE_SECONDS, OUTBOX_MAX_ATTEMPTS
from infrastructure.database import session_scope
from infrastructure.schema import OutboxEvent


class EventPublisher(Protocol):
    """类功能：`EventPublisher` 封装与“Outbox 数据访问”相关的数据结构、状态或行为。
    继承关系：继承 `Protocol`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def publish_task(self, event_id: str, payload: dict[str, Any]) -> str:
        """函数功能：`EventPublisher.publish_task` 在类 `EventPublisher` 中负责发布 task，服务于本文件职责：Outbox 数据访问。
        传参：
            event_id: 事件标识，用于外部事件幂等和审计，类型为 `str`。
            payload: 结构化载荷，通常来自事件、任务或 API 请求，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        ...


def claim_outbox_batch(
    *,
    worker_id: str,
    limit: int = 50,
    event_ids: list[str] | None = None,
    lease_seconds: int = OUTBOX_LEASE_SECONDS,
) -> list[dict[str, Any]]:
    """函数功能：`claim_outbox_batch` 负责认领 outbox batch，服务于本文件职责：Outbox 数据访问。
    传参：
        worker_id: worker id 参数，由调用方传入，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `50`。
        event_ids: event ids 参数，由调用方传入，类型为 `list[str] | None`，默认值为 `None`。
        lease_seconds: lease seconds 参数，由调用方传入，类型为 `int`，默认值为 `OUTBOX_LEASE_SECONDS`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    now = datetime.now().astimezone()
    with session_scope() as session:
        statement = select(OutboxEvent).where(
            OutboxEvent.published_at.is_(None),
            or_(
                (
                    OutboxEvent.status.in_(("pending", "retry"))
                    & or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now)
                ),
                (OutboxEvent.status == "publishing") & (OutboxEvent.lease_expires_at <= now),
            ),
        )
        if event_ids is not None:
            statement = statement.where(OutboxEvent.id.in_(event_ids))
        rows = list(
            session.execute(
                statement.order_by(OutboxEvent.created_at)
                .limit(max(1, int(limit)))
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        claimed: list[dict[str, Any]] = []
        for row in rows:
            token = uuid.uuid4().hex
            row.status = "publishing"
            row.claimed_by = worker_id
            row.lease_token = token
            row.lease_expires_at = now + timedelta(seconds=max(1, int(lease_seconds)))
            row.last_attempt_at = now
            row.publish_attempt_count += 1
            if not row.max_attempts:
                row.max_attempts = OUTBOX_MAX_ATTEMPTS
            claimed.append(
                {
                    "id": row.id,
                    "payload": dict(row.payload_json or {}),
                    "lease_token": token,
                    "attempt": row.publish_attempt_count,
                    "max_attempts": row.max_attempts,
                }
            )
        return claimed


def mark_outbox_published(event_id: str, lease_token: str) -> bool:
    """函数功能：`mark_outbox_published` 负责标记 outbox published，服务于本文件职责：Outbox 数据访问。
    传参：
        event_id: 事件标识，用于外部事件幂等和审计，类型为 `str`。
        lease_token: lease token 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    now = datetime.now().astimezone()
    with session_scope() as session:
        event_id_value = session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == "publishing",
                OutboxEvent.lease_token == lease_token,
            )
            .values(
                status="published",
                published_at=now,
                claimed_by=None,
                lease_token=None,
                lease_expires_at=None,
                next_attempt_at=None,
                last_error=None,
            )
            .returning(OutboxEvent.id)
        ).scalar_one_or_none()
        return event_id_value is not None


def mark_outbox_failed(event_id: str, lease_token: str, error: str) -> str:
    """函数功能：`mark_outbox_failed` 负责标记 outbox failed，服务于本文件职责：Outbox 数据访问。
    传参：
        event_id: 事件标识，用于外部事件幂等和审计，类型为 `str`。
        lease_token: lease token 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    now = datetime.now().astimezone()
    with session_scope() as session:
        row = session.execute(
            select(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == "publishing",
                OutboxEvent.lease_token == lease_token,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            return "stale"
        exhausted = row.publish_attempt_count >= max(1, int(row.max_attempts or OUTBOX_MAX_ATTEMPTS))
        row.status = "dead" if exhausted else "retry"
        row.failed_at = now if exhausted else None
        row.next_attempt_at = None if exhausted else now + timedelta(seconds=min(300, 2 ** max(0, row.publish_attempt_count - 1)))
        row.last_error = error[:2000]
        row.claimed_by = None
        row.lease_token = None
        row.lease_expires_at = None
        return row.status


def relay_outbox_batch(
    publisher: EventPublisher,
    *,
    limit: int = 50,
    event_ids: list[str] | None = None,
    worker_id: str | None = None,
) -> dict[str, int]:
    """函数功能：`relay_outbox_batch` 负责处理 relay outbox batch，服务于本文件职责：Outbox 数据访问。
    传参：
        publisher: publisher 参数，由调用方传入，类型为 `EventPublisher`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `50`。
        event_ids: event ids 参数，由调用方传入，类型为 `list[str] | None`，默认值为 `None`。
        worker_id: worker id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, int]`，表示结构化结果、载荷或状态映射。
    """
    relay_id = worker_id or f"{socket.gethostname()}-outbox-{uuid.uuid4().hex[:8]}"
    events = claim_outbox_batch(worker_id=relay_id, limit=limit, event_ids=event_ids)
    report = {"published": 0, "failed": 0, "dead": 0, "stale": 0}
    for event in events:
        task_id = str((event.get("payload") or {}).get("task_id") or "")
        task_type = str((event.get("payload") or {}).get("task_type") or "")
        event_extra = {
            "event_id": str(event["id"]),
            "task_id": task_id or None,
            "task_type": task_type or None,
            "worker_id": relay_id,
            "attempt": int(event.get("attempt") or 0),
            "max_attempts": int(event.get("max_attempts") or 0),
        }
        try:
            redis_message_id = publisher.publish_task(str(event["id"]), dict(event["payload"]))
        except Exception as exc:
            status = mark_outbox_failed(
                str(event["id"]),
                str(event["lease_token"]),
                f"{type(exc).__name__}: {exc}",
            )
            if status == "stale":
                report["stale"] += 1
            elif status == "dead":
                report["dead"] += 1
                report["failed"] += 1
            else:
                report["failed"] += 1
            log_event(
                "runtime.outbox_publish_failed",
                level="error" if status == "dead" else "warning",
                status=status,
                record_id=str(event["id"]),
                error=type(exc).__name__,
                extra=event_extra,
            )
            continue
        if mark_outbox_published(str(event["id"]), str(event["lease_token"])):
            report["published"] += 1
            log_event(
                "runtime.outbox_published",
                status="published",
                record_id=str(event["id"]),
                extra={**event_extra, "redis_message_id": redis_message_id},
            )
        else:
            report["stale"] += 1
            log_event(
                "runtime.outbox_publish_failed",
                level="warning",
                status="stale",
                record_id=str(event["id"]),
                extra={**event_extra, "redis_message_id": redis_message_id},
            )
    return report
