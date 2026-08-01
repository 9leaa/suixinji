"""文件作用：本地 delivery 去重存储。

项目关系：本文件依赖 `core.observability`、`core.settings`、`repositories.postgres`、`runtime.task`；被 `apps.handlers`、`apps.worker`、`bot.feishu_bot`、`repositories.postgres.delivery` 等 12 个模块。
"""



from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.observability import log_event
from core.settings import DELIVERY_MAX_ATTEMPTS, DELIVERY_RESERVATION_TTL_SECONDS
from runtime.task import now_iso


DATA_DIR = Path("data")
DELIVERY_DIR = DATA_DIR / "deliveries"
DELIVERY_PATH = DELIVERY_DIR / "index.json"

DELIVERY_RESERVED = "reserved"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"
DELIVERY_UNKNOWN = "unknown"

_LOCK = threading.RLock()


@dataclass
class DeliveryRecord:
    """类功能：`DeliveryRecord` 封装与“本地 delivery 去重存储”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    delivery_key: str
    delivery_type: str
    space_id: str
    message_id: str | None
    status: str
    created_at: str
    updated_at: str
    reserved_at: str | None = None
    lease_expires_at: str | None = None
    attempt_count: int = 0
    error: str | None = None


def reserve_delivery(
    delivery_key: str,
    *,
    delivery_type: str,
    space_id: str,
    message_id: str | None = None,
    tenant_id: str = "default",
) -> DeliveryRecord | None:
    """函数功能：`reserve_delivery` 负责预约 delivery，服务于本文件职责：本地 delivery 去重存储。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
        delivery_type: delivery type 参数，由调用方传入，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`，默认值为 `'default'`。
    返回结果说明：
        返回 `DeliveryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    del tenant_id
    with _LOCK:
        items = _load_raw()
        old = items.get(delivery_key)
        if old is not None:
            old_record = _record_from_raw(old)
            if old_record.status in {DELIVERY_SENT, DELIVERY_UNKNOWN}:
                return None
            if old_record.status == DELIVERY_RESERVED and not is_reservation_expired(old_record):
                return None
            if old_record.attempt_count >= DELIVERY_MAX_ATTEMPTS:
                log_event(
                    "runtime.delivery_exhausted",
                    level="warning",
                    status="skipped",
                    space_id=old_record.space_id,
                    message_id=old_record.message_id,
                    error="delivery max attempts exhausted",
                    extra={"delivery_key": delivery_key, "attempt_count": old_record.attempt_count},
                )
                return None

        now = now_iso()
        attempt_count = 1
        if old is not None:
            attempt_count = _record_from_raw(old).attempt_count + 1
        record = DeliveryRecord(
            delivery_key=delivery_key,
            delivery_type=delivery_type,
            space_id=space_id,
            message_id=message_id,
            status=DELIVERY_RESERVED,
            created_at=str(old.get("created_at") if old else now),
            updated_at=now,
            reserved_at=now,
            lease_expires_at=_future_iso(DELIVERY_RESERVATION_TTL_SECONDS),
            attempt_count=attempt_count,
            error=None,
        )
        items[delivery_key] = asdict(record)
        _save_raw(items)
        return record


def mark_sent(delivery_key: str) -> None:
    """函数功能：`mark_sent` 负责标记 sent，服务于本文件职责：本地 delivery 去重存储。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    _update_status(delivery_key, DELIVERY_SENT, None)


def mark_failed(delivery_key: str, error: str) -> None:
    """函数功能：`mark_failed` 负责标记 failed，服务于本文件职责：本地 delivery 去重存储。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    _update_status(delivery_key, DELIVERY_FAILED, error)


def mark_unknown(delivery_key: str, error: str) -> None:
    """函数功能：`mark_unknown` 负责标记 unknown，服务于本文件职责：本地 delivery 去重存储。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    _update_status(delivery_key, DELIVERY_UNKNOWN, error)


def get_delivery(delivery_key: str) -> DeliveryRecord | None:
    """函数功能：`get_delivery` 负责获取 delivery，服务于本文件职责：本地 delivery 去重存储。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `DeliveryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    raw = _load_raw().get(delivery_key)
    if raw is None:
        return None
    return _record_from_raw(raw)


def is_reservation_expired(record: DeliveryRecord, now: datetime | None = None) -> bool:
    """函数功能：`is_reservation_expired` 负责判断是否为 reservation expired，服务于本文件职责：本地 delivery 去重存储。
    传参：
        record: 待处理或持久化的记录对象，类型为 `DeliveryRecord`。
        now: now 参数，由调用方传入，类型为 `datetime | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if record.status != DELIVERY_RESERVED:
        return False
    if not record.lease_expires_at:
        return True
    now = now or datetime.now().astimezone()
    return _parse_iso(record.lease_expires_at) <= now


def recover_stale_reserved_deliveries() -> int:
    """函数功能：`recover_stale_reserved_deliveries` 负责处理 recover stale reserved deliveries，服务于本文件职责：本地 delivery 去重存储。
    传参：
        无。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    recovered = 0
    with _LOCK:
        items = _load_raw()
        for key, raw in list(items.items()):
            record = _record_from_raw(raw)
            if record.status != DELIVERY_RESERVED or not is_reservation_expired(record):
                continue
            record.status = DELIVERY_FAILED
            record.updated_at = now_iso()
            record.error = "reservation lease expired"
            items[key] = asdict(record)
            recovered += 1
            log_event(
                "runtime.delivery_stale_reserved",
                level="warning",
                status="failed",
                space_id=record.space_id,
                message_id=record.message_id,
                error=record.error,
                extra={"delivery_key": key, "attempt_count": record.attempt_count},
            )
        if recovered:
            _save_raw(items)
    return recovered


def _update_status(delivery_key: str, status: str, error: str | None) -> None:
    """函数功能：`_update_status` 负责更新 status，服务于本文件职责：本地 delivery 去重存储。
    传参：
        delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str | None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with _LOCK:
        items = _load_raw()
        raw = items.get(delivery_key)
        if raw is None:
            return
        raw["status"] = status
        raw["updated_at"] = now_iso()
        raw["error"] = error
        items[delivery_key] = raw
        _save_raw(items)


def _load_raw() -> dict[str, dict[str, Any]]:
    """函数功能：`_load_raw` 负责加载 raw，服务于本文件职责：本地 delivery 去重存储。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, dict[str, Any]]`，表示结构化结果、载荷或状态映射。
    """
    with _LOCK:
        if not DELIVERY_PATH.exists():
            return {}
        return json.loads(DELIVERY_PATH.read_text(encoding="utf-8"))


def _save_raw(items: dict[str, dict[str, Any]]) -> None:
    """函数功能：`_save_raw` 负责保存 raw，服务于本文件职责：本地 delivery 去重存储。
    传参：
        items: 待遍历或处理的元素集合，类型为 `dict[str, dict[str, Any]]`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with _LOCK:
        DELIVERY_DIR.mkdir(parents=True, exist_ok=True)
        DELIVERY_PATH.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _record_from_raw(raw: dict[str, Any]) -> DeliveryRecord:
    """函数功能：`_record_from_raw` 负责记录 from raw，服务于本文件职责：本地 delivery 去重存储。
    传参：
        raw: raw 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `DeliveryRecord` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return DeliveryRecord(
        delivery_key=str(raw["delivery_key"]),
        delivery_type=str(raw["delivery_type"]),
        space_id=str(raw["space_id"]),
        message_id=raw.get("message_id"),
        status=str(raw["status"]),
        created_at=str(raw["created_at"]),
        updated_at=str(raw["updated_at"]),
        reserved_at=raw.get("reserved_at"),
        lease_expires_at=raw.get("lease_expires_at"),
        attempt_count=int(raw.get("attempt_count") or 0),
        error=raw.get("error"),
    )


def _future_iso(seconds: int) -> str:
    """函数功能：`_future_iso` 负责处理 future iso，服务于本文件职责：本地 delivery 去重存储。
    传参：
        seconds: seconds 参数，由调用方传入，类型为 `int`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def _parse_iso(value: str) -> datetime:
    """函数功能：`_parse_iso` 负责解析 iso，服务于本文件职责：本地 delivery 去重存储。
    传参：
        value: 待转换、校验或计算的值，类型为 `str`。
    返回结果说明：
        返回 `datetime` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def ingest_archived_key(space_id: str, message_id: str) -> str:
    """函数功能：`ingest_archived_key` 负责处理 ingest archived key，服务于本文件职责：本地 delivery 去重存储。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return f"ingest:{space_id}:{message_id}:archived"


def query_key(space_id: str, message_id: str) -> str:
    """函数功能：`query_key` 负责查询 key，服务于本文件职责：本地 delivery 去重存储。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return f"query:{space_id}:{message_id}"


def manual_summary_key(space_id: str, message_id: str) -> str:
    """函数功能：`manual_summary_key` 负责处理 manual summary key，服务于本文件职责：本地 delivery 去重存储。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return f"manual_summary:{space_id}:{message_id}"


def auto_summary_key(space_id: str, range_key: str, date: str) -> str:
    """函数功能：`auto_summary_key` 负责处理 auto summary key，服务于本文件职责：本地 delivery 去重存储。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        range_key: range key 参数，由调用方传入，类型为 `str`。
        date: date 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return f"auto_summary:{space_id}:{range_key}:{date}"


from core.settings import STORAGE_BACKEND as _STORAGE_BACKEND

if _STORAGE_BACKEND == "postgres":
    from repositories.postgres import delivery as _postgres_delivery

    reserve_delivery = _postgres_delivery.reserve_delivery
    mark_sent = _postgres_delivery.mark_sent
    mark_failed = _postgres_delivery.mark_failed
    mark_unknown = _postgres_delivery.mark_unknown
    get_delivery = _postgres_delivery.get_delivery
    is_reservation_expired = _postgres_delivery.is_reservation_expired
    recover_stale_reserved_deliveries = _postgres_delivery.recover_stale_reserved_deliveries
