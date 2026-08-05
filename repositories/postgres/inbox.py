"""文件作用：Inbox 数据访问。

项目关系：本文件依赖 `infrastructure.database`、`infrastructure.schema`、`repositories.postgres.common`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import InboxMessage
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime


def _as_record(row: InboxMessage) -> dict[str, Any]:
    """函数功能：`_as_record` 负责记录 as，服务于本文件职责：Inbox 数据访问。
    传参：
        row: row 参数，由调用方传入，类型为 `InboxMessage`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    return {
        "id": row.id,
        "source": row.source,
        "event_id": row.source_event_id,
        "message_id": row.source_message_id,
        "space_id": row.space_id,
        "chat_id": row.chat_id,
        "chat_type": row.chat_type,
        "sender": dict(row.sender_json or {}),
        "ts": row.received_at.isoformat(),
        "text": row.text,
        "status": row.status,
        "sensitivity": row.sensitivity,
    }


def append_message_once(record: Any) -> bool:
    """函数功能：`append_message_once` 负责追加 message once，服务于本文件职责：Inbox 数据访问。
    传参：
        record: 待处理或持久化的记录对象，类型为 `Any`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    values = asdict(record) if not isinstance(record, dict) else dict(record)
    source = str(values.get("source") or "feishu")
    space_id = str(values["space_id"])
    sender = dict(values.get("sender") or {})
    tenant_id = str(values.get("tenant_id") or sender.get("tenant_key") or DEFAULT_TENANT_ID)
    with session_scope() as session:
        space_id = ensure_tenant_space(session, space_id, tenant_id=tenant_id, source=source)
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:space_id))"), {"space_id": f"{tenant_id}:{space_id}"})
        next_sequence = int(
            session.execute(
                select(func.coalesce(func.max(InboxMessage.sequence_no), 0) + 1).where(InboxMessage.space_id == space_id)
            ).scalar_one()
        )
        result = session.execute(
            insert(InboxMessage)
            .values(
                id=str(values["id"]),
                source=source,
                source_message_id=str(values["message_id"]),
                source_event_id=values.get("event_id"),
                tenant_id=tenant_id,
                space_id=space_id,
                chat_id=values.get("chat_id"),
                chat_type=values.get("chat_type"),
                sender_json=sender,
                text=str(values.get("text") or ""),
                received_at=parse_datetime(values.get("ts")),
                status=str(values.get("status") or "pending"),
                sensitivity=str(values.get("sensitivity") or "normal"),
                sequence_no=next_sequence,
            )
            .on_conflict_do_nothing(constraint="uq_inbox_tenant_source_message")
            .returning(InboxMessage.id)
        ).scalar_one_or_none()
        return result is not None


def append_record(record: Any) -> None:
    """函数功能：`append_record` 负责追加 record，服务于本文件职责：Inbox 数据访问。
    传参：
        record: 待处理或持久化的记录对象，类型为 `Any`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    append_message_once(record)


def list_wal_space_ids() -> list[str]:
    """函数功能：`list_wal_space_ids` 负责列出 wal space ids，服务于本文件职责：Inbox 数据访问。
    传参：
        无。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    with session_scope() as session:
        return list(session.execute(select(InboxMessage.space_id).distinct().order_by(InboxMessage.space_id)).scalars())


def load_records(space_id: str) -> list[dict[str, Any]]:
    """函数功能：`load_records` 负责加载 records，服务于本文件职责：Inbox 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    with session_scope() as session:
        rows = session.execute(
            select(InboxMessage).where(InboxMessage.space_id == space_id).order_by(InboxMessage.sequence_no)
        ).scalars()
        return [_as_record(row) for row in rows]


def load_previous_user_messages(
    space_id: str,
    *,
    sequence_no: int,
    sender: dict[str, Any],
    tenant_id: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Load at most three earlier messages from the same tenant/space/user."""
    identity_keys = ("user_id", "open_id", "union_id", "source_user_id", "id")
    sender_id = next((str(sender.get(key) or "") for key in identity_keys if sender.get(key)), "")
    if not sender_id or sequence_no <= 0:
        return []
    bounded = max(1, min(int(limit), 3))
    with session_scope() as session:
        rows = list(
            session.execute(
                select(InboxMessage)
                .where(
                    InboxMessage.tenant_id == tenant_id,
                    InboxMessage.space_id == space_id,
                    InboxMessage.sequence_no < sequence_no,
                    InboxMessage.sensitivity == "normal",
                )
                .order_by(InboxMessage.sequence_no.desc())
                .limit(12)
            ).scalars()
        )
    matched: list[dict[str, Any]] = []
    for row in rows:
        previous_sender = dict(row.sender_json or {})
        previous_id = next((str(previous_sender.get(key) or "") for key in identity_keys if previous_sender.get(key)), "")
        sender_type = str(previous_sender.get("sender_type") or previous_sender.get("type") or "user").casefold()
        if previous_id != sender_id or sender_type in {"bot", "system", "assistant"}:
            continue
        matched.append({"note_id": row.id, "sequence_no": row.sequence_no, "text": row.text})
        if len(matched) >= bounded:
            break
    return [
        {**item, "offset": -(index + 1)}
        for index, item in enumerate(matched)
    ]


def message_exists(space_id: str, message_id: str) -> bool:
    """函数功能：`message_exists` 负责处理 message exists，服务于本文件职责：Inbox 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    with session_scope() as session:
        resolved_space_id = ensure_tenant_space(session, space_id)
        return session.execute(
            select(InboxMessage.id).where(
                InboxMessage.space_id == resolved_space_id,
                InboxMessage.source_message_id == message_id,
            ).limit(1)
        ).scalar_one_or_none() is not None


def load_pending_records(space_id: str) -> list[dict[str, Any]]:
    """函数功能：`load_pending_records` 负责加载 pending records，服务于本文件职责：Inbox 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    with session_scope() as session:
        rows = session.execute(
            select(InboxMessage)
            .where(InboxMessage.space_id == space_id, InboxMessage.status == "pending")
            .order_by(InboxMessage.sequence_no)
        ).scalars()
        return [_as_record(row) for row in rows]


def mark_processed(space_id: str, record_id: str) -> None:
    """函数功能：`mark_processed` 负责标记 processed，服务于本文件职责：Inbox 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        record_id: record id 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with session_scope() as session:
        session.execute(
            update(InboxMessage)
            .where(InboxMessage.space_id == space_id, InboxMessage.id == record_id)
            .values(status="processed")
        )


def mark_sensitive_blocked(
    space_id: str,
    record_id: str,
    category: str = "sensitive",
    *,
    preserve_pending: bool = False,
) -> None:
    """函数功能：`mark_sensitive_blocked` 负责标记 sensitive blocked，服务于本文件职责：Inbox 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        record_id: record id 参数，由调用方传入，类型为 `str`。
        category: category 参数，由调用方传入，类型为 `str`，默认值为 `'sensitive'`。
        preserve_pending: preserve pending 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with session_scope() as session:
        session.execute(
            update(InboxMessage)
            .where(InboxMessage.space_id == space_id, InboxMessage.id == record_id)
            .values(
                text="[敏感内容已拦截，原文未保存]",
                status="pending" if preserve_pending else "blocked_sensitive",
                sensitivity=category or "sensitive",
            )
        )
