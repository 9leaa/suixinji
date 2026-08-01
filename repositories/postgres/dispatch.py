"""文件作用：接收、投递和水位线事务。

项目关系：本文件依赖 `infrastructure.database`、`infrastructure.schema`、`memory.models`、`repositories.postgres.common` 等 5 个模块；被 `agent.hooks.task_dispatch`、`apps.handlers`、`apps.receiver`、`apps.scheduler` 等 13 个模块。
"""



from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import InboxMessage, OutboxEvent, Space, Task
from memory.models import new_id
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime
from runtime.consistency import task_consistency


@dataclass(frozen=True)
class DispatchResult:
    """类功能：`DispatchResult` 封装与“接收、投递和水位线事务”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    inbox_id: str
    task_id: str | None
    created: bool
    duplicate: bool
    in_progress: bool = False


def _publish_task_request(session: Any, task: Task | str, task_type: str | None = None, *, attempt: int = 1) -> str:
    """函数功能：`_publish_task_request` 负责发布 task request，服务于本文件职责：接收、投递和水位线事务。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        task: task 参数，由调用方传入，类型为 `Task | str`。
        task_type: task type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        attempt: attempt 参数，由调用方传入，类型为 `int`，默认值为 `1`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    task_id = str(task.id if isinstance(task, Task) else task)
    resolved_type = str(task.task_type if isinstance(task, Task) else task_type or "")
    event_id = new_id("event")
    session.add(
        OutboxEvent(
            id=event_id,
            event_type="task.requested",
            aggregate_type="task",
            aggregate_id=task_id,
            payload_json={"task_id": task_id, "task_type": resolved_type, "attempt": max(1, int(attempt))},
        )
    )
    return event_id


def _enqueue_task_in_session(
    session: Any,
    *,
    task_type: str,
    tenant_id: str,
    space_id: str,
    source_message_id: str | None,
    idempotency_key: str,
    payload: dict[str, Any],
    priority: int = 0,
    max_attempts: int = 5,
    initial_status: str = "queued",
    publish: bool = True,
) -> tuple[str, bool]:
    """函数功能：`_enqueue_task_in_session` 负责处理 enqueue task in session，服务于本文件职责：接收、投递和水位线事务。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        task_type: task type 参数，由调用方传入，类型为 `str`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        source_message_id: source message id 参数，由调用方传入，类型为 `str | None`。
        idempotency_key: idempotency key 参数，由调用方传入，类型为 `str`。
        payload: 结构化载荷，通常来自事件、任务或 API 请求，类型为 `dict[str, Any]`。
        priority: priority 参数，由调用方传入，类型为 `int`，默认值为 `0`。
        max_attempts: max attempts 参数，由调用方传入，类型为 `int`，默认值为 `5`。
        initial_status: initial status 参数，由调用方传入，类型为 `str`，默认值为 `'queued'`。
        publish: publish 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
    返回结果说明：
        返回 `tuple[str, bool]`，表示由多个相关值组成的结果。
    """
    if publish and initial_status != "queued":
        raise ValueError("only queued tasks may be published")
    task_id = new_id("task")
    created_id = session.execute(
        insert(Task)
        .values(
            id=task_id,
            task_type=task_type,
            tenant_id=tenant_id,
            space_id=space_id,
            source_message_id=source_message_id,
            idempotency_key=idempotency_key,
            status=initial_status,
            priority=priority,
            attempt_count=0,
            failure_count=0,
            defer_count=0,
            max_attempts=max_attempts,
            payload_json=payload,
        )
        .on_conflict_do_nothing(index_elements=[Task.idempotency_key])
        .returning(Task.id)
    ).scalar_one_or_none()
    if created_id is None:
        existing = session.execute(select(Task.id).where(Task.idempotency_key == idempotency_key)).scalar_one()
        return str(existing), False
    if publish:
        _publish_task_request(session, task_id, task_type, attempt=1)
    return task_id, True


def enqueue_task(
    *,
    task_type: str,
    space_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    tenant_id: str = DEFAULT_TENANT_ID,
    source_message_id: str | None = None,
    priority: int = 0,
    max_attempts: int = 5,
    initial_status: str = "queued",
    publish: bool = True,
) -> tuple[str, bool]:
    """函数功能：`enqueue_task` 负责处理 enqueue task，服务于本文件职责：接收、投递和水位线事务。
    传参：
        task_type: task type 参数，由调用方传入，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        idempotency_key: idempotency key 参数，由调用方传入，类型为 `str`。
        payload: 结构化载荷，通常来自事件、任务或 API 请求，类型为 `dict[str, Any]`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`，默认值为 `DEFAULT_TENANT_ID`。
        source_message_id: source message id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        priority: priority 参数，由调用方传入，类型为 `int`，默认值为 `0`。
        max_attempts: max attempts 参数，由调用方传入，类型为 `int`，默认值为 `5`。
        initial_status: initial status 参数，由调用方传入，类型为 `str`，默认值为 `'queued'`。
        publish: publish 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
    返回结果说明：
        返回 `tuple[str, bool]`，表示由多个相关值组成的结果。
    """
    with session_scope() as session:
        space_id = ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        return _enqueue_task_in_session(
            session,
            task_type=task_type,
            tenant_id=tenant_id,
            space_id=space_id,
            source_message_id=source_message_id,
            idempotency_key=idempotency_key,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
            initial_status=initial_status,
            publish=publish,
        )


def receive_command(
    *,
    source: str,
    source_message_id: str,
    source_event_id: str | None,
    tenant_id: str,
    space_id: str,
    chat_id: str | None,
    chat_type: str | None,
    sender: dict[str, Any],
    text_value: str,
    received_at: str | datetime,
    task_type: str,
    task_payload: dict[str, Any],
    sensitivity: str = "normal",
    max_attempts: int = 5,
) -> DispatchResult:
    """函数功能：`receive_command` 负责接收 command，服务于本文件职责：接收、投递和水位线事务。
    传参：
        source: source 参数，由调用方传入，类型为 `str`。
        source_message_id: source message id 参数，由调用方传入，类型为 `str`。
        source_event_id: source event id 参数，由调用方传入，类型为 `str | None`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        chat_id: chat id 参数，由调用方传入，类型为 `str | None`。
        chat_type: chat type 参数，由调用方传入，类型为 `str | None`。
        sender: sender 参数，由调用方传入，类型为 `dict[str, Any]`。
        text_value: text value 参数，由调用方传入，类型为 `str`。
        received_at: received at 参数，由调用方传入，类型为 `str | datetime`。
        task_type: task type 参数，由调用方传入，类型为 `str`。
        task_payload: task payload 参数，由调用方传入，类型为 `dict[str, Any]`。
        sensitivity: sensitivity 参数，由调用方传入，类型为 `str`，默认值为 `'normal'`。
        max_attempts: max attempts 参数，由调用方传入，类型为 `int`，默认值为 `5`。
    返回结果说明：
        返回 `DispatchResult` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    tenant_id = tenant_id or DEFAULT_TENANT_ID
    with session_scope() as session:
        source_space_id = space_id
        space_id = ensure_tenant_space(session, source_space_id, tenant_id=tenant_id, source=source)
        lock_key = f"{tenant_id}:{space_id}"
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:space_id))"), {"space_id": lock_key})
        existing = session.execute(
            select(InboxMessage).where(
                InboxMessage.tenant_id == tenant_id,
                InboxMessage.source == source,
                InboxMessage.source_message_id == source_message_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            task = session.execute(
                select(Task.id).where(Task.idempotency_key == f"{tenant_id}:{task_type}:{source}:{source_message_id}")
            ).scalar_one_or_none()
            return DispatchResult(existing.id, str(task) if task else None, False, True)

        sequence_no = int(
            session.execute(
                select(func.coalesce(func.max(InboxMessage.sequence_no), 0) + 1).where(InboxMessage.space_id == space_id)
            ).scalar_one()
        )
        inbox_id = new_id("inbox")
        consistency = task_consistency(task_type, task_payload)
        now = parse_datetime(received_at)
        is_ingest = task_type == "ingest"
        session.add(
            InboxMessage(
                id=inbox_id,
                source=source,
                source_message_id=source_message_id,
                source_event_id=source_event_id,
                tenant_id=tenant_id,
                space_id=space_id,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_json=sender,
                text=text_value,
                received_at=parse_datetime(received_at),
                status="pending",
                sensitivity=sensitivity,
                sequence_no=sequence_no,
                note_status="pending" if is_ingest else "completed",
                memory_status="pending" if is_ingest else "completed",
                note_completed_at=None if is_ingest else now,
                memory_completed_at=None if is_ingest else now,
            )
        )
        space = session.execute(select(Space).where(Space.id == space_id).with_for_update()).scalar_one()
        required_watermark = max(0, sequence_no - 1)
        current_watermark = int(space.memory_watermark if consistency == "memory" else space.note_watermark)
        initial_status = "blocked" if consistency in {"note", "memory"} and current_watermark < required_watermark else "queued"
        task_payload = {
            **task_payload,
            "source_space_id": source_space_id,
            "inbox_id": inbox_id,
            "sequence_no": sequence_no,
            "consistency": consistency,
            "required_watermark": required_watermark,
        }
        task_id, _ = _enqueue_task_in_session(
            session,
            task_type=task_type,
            tenant_id=tenant_id,
            space_id=space_id,
            source_message_id=source_message_id,
            idempotency_key=f"{tenant_id}:{task_type}:{source}:{source_message_id}",
            payload=task_payload,
            max_attempts=max_attempts,
            initial_status=initial_status,
            publish=initial_status == "queued",
        )
        session.flush()
        if not is_ingest:
            _advance_watermarks_in_session(session, space)
        return DispatchResult(inbox_id, task_id, True, False)


def load_inbox_record(inbox_id: str) -> dict[str, Any] | None:
    """函数功能：`load_inbox_record` 负责加载 inbox record，服务于本文件职责：接收、投递和水位线事务。
    传参：
        inbox_id: inbox id 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    with session_scope() as session:
        row = session.get(InboxMessage, inbox_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "source": row.source,
            "event_id": row.source_event_id,
            "message_id": row.source_message_id,
            "tenant_id": row.tenant_id,
            "space_id": row.space_id,
            "chat_id": row.chat_id,
            "chat_type": row.chat_type,
            "sender": dict(row.sender_json or {}),
            "ts": row.received_at.isoformat(),
            "text": row.text,
            "status": row.status,
            "sensitivity": row.sensitivity,
            "sequence_no": row.sequence_no,
        }


def is_next_inbox_message(inbox_id: str) -> bool:
    """函数功能：`is_next_inbox_message` 负责判断是否为 next inbox message，服务于本文件职责：接收、投递和水位线事务。
    传参：
        inbox_id: inbox id 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    with session_scope() as session:
        row = session.get(InboxMessage, inbox_id)
        if row is None:
            return False
        earlier = session.execute(
            select(InboxMessage.id)
            .where(
                InboxMessage.space_id == row.space_id,
                InboxMessage.sequence_no < row.sequence_no,
                InboxMessage.status == "pending",
            )
            .limit(1)
        ).scalar_one_or_none()
        return earlier is None


def activate_task_in_session(session: Any, task_id: str) -> str | None:
    """函数功能：`activate_task_in_session` 负责处理 activate task in session，服务于本文件职责：接收、投递和水位线事务。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        task_id: 任务标识，用于查询、更新或幂等处理任务状态，类型为 `str`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    row = session.execute(select(Task).where(Task.id == task_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise ValueError(f"task not found: {task_id}")
    if row.status != "blocked":
        return None
    row.status = "queued"
    row.next_retry_at = None
    row.claimed_by = None
    row.lease_token = None
    row.lease_expires_at = None
    _publish_task_request(session, row, attempt=row.attempt_count + 1)
    return row.id


def _activate_ready_tasks_in_session(session: Any, space: Space) -> int:
    """函数功能：`_activate_ready_tasks_in_session` 负责处理 activate ready tasks in session，服务于本文件职责：接收、投递和水位线事务。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        space: space 参数，由调用方传入，类型为 `Space`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    rows = list(
        session.execute(
            select(Task)
            .where(
                Task.space_id == space.id,
                Task.status == "blocked",
                or_(
                    ~Task.payload_json["consistency"].as_string().in_(["note", "memory"]),
                    and_(
                        Task.payload_json["consistency"].as_string() == "note",
                        Task.payload_json["required_watermark"].as_integer() <= int(space.note_watermark or 0),
                    ),
                    and_(
                        Task.payload_json["consistency"].as_string() == "memory",
                        Task.payload_json["required_watermark"].as_integer() <= int(space.memory_watermark or 0),
                    ),
                ),
            )
            .order_by(Task.created_at, Task.id)
            .with_for_update()
        ).scalars()
    )
    activated = 0
    for row in rows:
        row.status = "queued"
        row.next_retry_at = None
        row.claimed_by = None
        row.lease_token = None
        row.lease_expires_at = None
        _publish_task_request(session, row, attempt=row.attempt_count + 1)
        activated += 1
    return activated


def _advance_watermarks_in_session(session: Any, space: Space) -> None:
    """函数功能：`_advance_watermarks_in_session` 负责处理 advance watermarks in session，服务于本文件职责：接收、投递和水位线事务。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        space: space 参数，由调用方传入，类型为 `Space`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    note_current = int(space.note_watermark or 0)
    memory_current = int(space.memory_watermark or 0)
    note_open = func.min(InboxMessage.sequence_no).filter(
        InboxMessage.sequence_no > note_current,
        ~InboxMessage.note_status.in_(["completed", "failed"]),
    )
    note_max = func.max(InboxMessage.sequence_no).filter(InboxMessage.sequence_no > note_current)
    memory_open = func.min(InboxMessage.sequence_no).filter(
        InboxMessage.sequence_no > memory_current,
        ~InboxMessage.memory_status.in_(["completed", "failed"]),
    )
    memory_max = func.max(InboxMessage.sequence_no).filter(InboxMessage.sequence_no > memory_current)
    first_note_open, last_note, first_memory_open, last_memory = session.execute(
        select(note_open, note_max, memory_open, memory_max).where(InboxMessage.space_id == space.id)
    ).one()
    if first_note_open is not None:
        space.note_watermark = max(note_current, int(first_note_open) - 1)
    elif last_note is not None:
        space.note_watermark = max(note_current, int(last_note))
    if first_memory_open is not None:
        space.memory_watermark = max(memory_current, int(first_memory_open) - 1)
    elif last_memory is not None:
        space.memory_watermark = max(memory_current, int(last_memory))
    _activate_ready_tasks_in_session(session, space)


def skip_inbox_record(
    inbox_id: str,
    *,
    final_status: str,
    cancel_root_task: bool = False,
) -> bool:
    """函数功能：`skip_inbox_record` 负责记录 skip inbox，服务于本文件职责：接收、投递和水位线事务。
    传参：
        inbox_id: inbox id 参数，由调用方传入，类型为 `str`。
        final_status: final status 参数，由调用方传入，类型为 `str`。
        cancel_root_task: cancel root task 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    with session_scope() as session:
        inbox = session.execute(select(InboxMessage).where(InboxMessage.id == inbox_id).with_for_update()).scalar_one_or_none()
        if inbox is None:
            return False
        space = session.execute(select(Space).where(Space.id == inbox.space_id).with_for_update()).scalar_one()
        now = datetime.now().astimezone()
        inbox.status = final_status
        inbox.note_status = "completed"
        inbox.memory_status = "completed"
        inbox.note_completed_at = now
        inbox.memory_completed_at = now
        if cancel_root_task:
            session.execute(
                text(
                    "UPDATE tasks SET status = 'cancelled', completed_at = :now, last_error = :reason "
                    "WHERE source_message_id = :message_id AND task_type = 'ingest' "
                    "AND status IN ('blocked', 'queued', 'retry')"
                ),
                {"now": now, "reason": final_status, "message_id": inbox.source_message_id},
            )
        _advance_watermarks_in_session(session, space)
        return True


def complete_blocked_sensitive_inbox(inbox_id: str) -> bool:
    """函数功能：`complete_blocked_sensitive_inbox` 负责完成 blocked sensitive inbox，服务于本文件职责：接收、投递和水位线事务。
    传参：
        inbox_id: inbox id 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return skip_inbox_record(inbox_id, final_status="blocked_sensitive")


def recover_skipped_ingress(*, limit: int = 500) -> dict[str, int]:
    """函数功能：`recover_skipped_ingress` 负责处理 recover skipped ingress，服务于本文件职责：接收、投递和水位线事务。
    传参：
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `500`。
    返回结果说明：
        返回 `dict[str, int]`，表示结构化结果、载荷或状态映射。
    """
    with session_scope() as session:
        sensitive_ids = list(
            session.execute(
                select(InboxMessage.id)
                .where(
                    InboxMessage.status == "blocked_sensitive",
                    ~InboxMessage.note_status.in_(["completed", "failed"]),
                )
                .order_by(InboxMessage.received_at)
                .limit(max(1, min(int(limit), 5000)))
            ).scalars()
        )
        unknown_command_ids = list(
            session.execute(
                select(InboxMessage.id)
                .join(Task, Task.source_message_id == InboxMessage.source_message_id)
                .where(
                    InboxMessage.status == "pending",
                    InboxMessage.text.startswith("/"),
                    Task.task_type == "ingest",
                    Task.status.in_(("blocked", "queued", "retry")),
                )
                .order_by(InboxMessage.received_at)
                .limit(max(1, min(int(limit), 5000)))
            ).scalars()
        )
    for inbox_id in sensitive_ids:
        complete_blocked_sensitive_inbox(str(inbox_id))
    for inbox_id in unknown_command_ids:
        skip_inbox_record(str(inbox_id), final_status="skipped_command", cancel_root_task=True)
    return {"sensitive": len(sensitive_ids), "unknown_commands": len(unknown_command_ids)}


def complete_inbox_stage_in_session(
    session: Any,
    inbox_id: str,
    *,
    note: bool = False,
    memory: bool = False,
    finalize: bool = False,
    success: bool = True,
    error: str | None = None,
) -> None:
    """函数功能：`complete_inbox_stage_in_session` 负责完成 inbox stage in session，服务于本文件职责：接收、投递和水位线事务。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        inbox_id: inbox id 参数，由调用方传入，类型为 `str`。
        note: note 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        memory: memory 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        finalize: finalize 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        success: success 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    inbox = session.execute(
        select(InboxMessage).where(InboxMessage.id == inbox_id).with_for_update()
    ).scalar_one_or_none()
    if inbox is None:
        raise ValueError(f"inbox record not found: {inbox_id}")
    if finalize:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:space_id))"),
            {"space_id": f"{inbox.tenant_id}:{inbox.space_id}"},
        )
    space = session.execute(select(Space).where(Space.id == inbox.space_id).with_for_update()).scalar_one()
    now = datetime.now().astimezone()
    stage_status = "completed" if success else "failed"
    if note:
        inbox.note_status = stage_status
        inbox.note_completed_at = now
        if not success:
            metadata = dict(space.metadata_json or {})
            metadata["last_note_gap"] = {
                "sequence_no": int(inbox.sequence_no),
                "inbox_id": inbox.id,
                "error": str(error or "unknown")[:256],
            }
            space.metadata_json = metadata
    if memory:
        inbox.memory_status = stage_status
        inbox.memory_completed_at = now
        if not success:
            space.memory_gap_sequence_no = int(inbox.sequence_no)
            metadata = dict(space.metadata_json or {})
            metadata["last_memory_gap"] = {
                "sequence_no": int(inbox.sequence_no),
                "inbox_id": inbox.id,
                "error_type": str(error or "unknown").split(":", 1)[0][:128],
            }
            space.metadata_json = metadata
    if finalize and inbox.status not in {"processed", "failed"}:
        inbox.status = "processed" if success else "failed"
        space.processed_sequence_no = max(int(space.processed_sequence_no or 0), int(inbox.sequence_no))
        if not success:
            if inbox.note_status == "pending":
                inbox.note_status = "failed"
                inbox.note_completed_at = now
            if inbox.memory_status == "pending":
                inbox.memory_status = "failed"
                inbox.memory_completed_at = now
                space.memory_gap_sequence_no = int(inbox.sequence_no)
                metadata = dict(space.metadata_json or {})
                metadata["last_memory_gap"] = {
                    "sequence_no": int(inbox.sequence_no),
                    "inbox_id": inbox.id,
                    "error_type": str(error or "unknown").split(":", 1)[0][:128],
                }
                space.metadata_json = metadata
    _advance_watermarks_in_session(session, space)


def mark_inbox_note_completed_in_session(
    session: Any,
    inbox_id: str,
    *,
    success: bool = True,
    error: str | None = None,
) -> None:
    """函数功能：`mark_inbox_note_completed_in_session` 负责标记 inbox note completed in session，服务于本文件职责：接收、投递和水位线事务。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        inbox_id: inbox id 参数，由调用方传入，类型为 `str`。
        success: success 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    complete_inbox_stage_in_session(session, inbox_id, note=True, success=success, error=error)


def mark_inbox_memory_completed_in_session(
    session: Any,
    inbox_id: str,
    *,
    success: bool = True,
    error: str | None = None,
) -> None:
    """函数功能：`mark_inbox_memory_completed_in_session` 负责标记 inbox memory completed in session，服务于本文件职责：接收、投递和水位线事务。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        inbox_id: inbox id 参数，由调用方传入，类型为 `str`。
        success: success 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    complete_inbox_stage_in_session(session, inbox_id, memory=True, success=success, error=error)


def task_watermark_ready(task: dict[str, Any]) -> bool:
    """函数功能：`task_watermark_ready` 负责处理 task watermark ready，服务于本文件职责：接收、投递和水位线事务。
    传参：
        task: task 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    payload = dict(task.get("payload_json") or {})
    consistency = str(payload.get("consistency") or "weak")
    if consistency not in {"note", "memory"}:
        return True
    required = int(payload.get("required_watermark") or 0)
    with session_scope() as session:
        space = session.get(Space, str(task["space_id"]))
        if space is None:
            return False
        current = int(space.memory_watermark if consistency == "memory" else space.note_watermark)
        return current >= required


def _root_task_for_inbox(session: Any, inbox: InboxMessage) -> Task | None:
    """函数功能：`_root_task_for_inbox` 负责处理 root task for inbox，服务于本文件职责：接收、投递和水位线事务。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        inbox: inbox 参数，由调用方传入，类型为 `InboxMessage`。
    返回结果说明：
        返回 `Task | None`；未命中或无需处理时可返回 `None`。
    """
    return session.execute(
        select(Task)
        .where(
            Task.tenant_id == inbox.tenant_id,
            Task.space_id == inbox.space_id,
            Task.source_message_id == inbox.source_message_id,
            Task.payload_json["inbox_id"].as_string() == inbox.id,
        )
        .with_for_update()
    ).scalar_one_or_none()


def finalize_inbox_in_session(
    session: Any,
    inbox_id: str,
    *,
    success: bool,
    error: str | None = None,
) -> str | None:
    """函数功能：`finalize_inbox_in_session` 负责处理 finalize inbox in session，服务于本文件职责：接收、投递和水位线事务。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        inbox_id: inbox id 参数，由调用方传入，类型为 `str`。
        success: success 参数，由调用方传入，类型为 `bool`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    complete_inbox_stage_in_session(session, inbox_id, finalize=True, success=success, error=error)
    return None


def mark_inbox_processed(inbox_id: str) -> str | None:
    """函数功能：`mark_inbox_processed` 负责标记 inbox processed，服务于本文件职责：接收、投递和水位线事务。
    传参：
        inbox_id: inbox id 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    with session_scope() as session:
        return finalize_inbox_in_session(session, inbox_id, success=True)


def mark_inbox_failed(inbox_id: str, error: str) -> str | None:
    """函数功能：`mark_inbox_failed` 负责标记 inbox failed，服务于本文件职责：接收、投递和水位线事务。
    传参：
        inbox_id: inbox id 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    with session_scope() as session:
        return finalize_inbox_in_session(session, inbox_id, success=False, error=error)


def get_space_progress(space_id: str) -> dict[str, int | None] | None:
    """函数功能：`get_space_progress` 负责获取 space progress，服务于本文件职责：接收、投递和水位线事务。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `dict[str, int | None] | None`，表示结构化结果、载荷或状态映射。
    """
    with session_scope() as session:
        row = session.get(Space, space_id)
        if row is None:
            return None
        return {
            "processed_sequence_no": int(row.processed_sequence_no or 0),
            "note_watermark": int(row.note_watermark or 0),
            "memory_watermark": int(row.memory_watermark or 0),
            "memory_gap_sequence_no": int(row.memory_gap_sequence_no) if row.memory_gap_sequence_no is not None else None,
        }
