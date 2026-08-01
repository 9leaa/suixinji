"""文件作用：Memory 数据访问。

项目关系：本文件依赖 `core.llm_client`、`core.settings`、`infrastructure.database`、`infrastructure.redis_cache` 等 15 个模块；被 `apps.handlers`、`eval.large_live_retrieval_eval`、`eval.live_retrieval_eval`、`memory.trace` 等 6 个模块。
"""



from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from datetime import date, datetime
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from core.settings import (
    COORDINATION_BACKEND,
    FAKE_EXTERNALS,
    MEMORY_ACCESS_BUFFER_ENABLED,
    MEMORY_ACCESS_FLUSH_BATCH_SIZE,
    MEMORY_CONSOLIDATION_RUN_LEASE_SECONDS,
    MEMORY_HYBRID_RRF_K,
    MEMORY_HYBRID_VECTOR_ENABLED,
    MEMORY_TRIGRAM_ENABLED,
    MEMORY_UNIFIED_RERANK_ENABLED,
    MEMORY_QUERY_MIN_SCORE,
    MEMORY_RETRIEVAL_MODE,
    MEMORY_VECTOR_LIFECYCLE_ENABLED,
    MEMORY_VECTOR_MAX_ATTEMPTS,
    MEMORY_VECTOR_RETRY_BASE_SECONDS,
    RETRIEVAL_WEIGHTED_RRF_ENABLED,
)
from infrastructure.database import session_scope
from infrastructure.schema import (
    Memory,
    MemoryCandidateRow,
    MemoryConsolidationRun,
    MemoryDecision as MemoryDecisionRow,
    MemoryExtractionState as MemoryExtractionStateRow,
    MemoryRelation as MemoryRelationRow,
    MemorySource as MemorySourceRow,
    MemoryTrace,
    MemoryVector,
    MemoryVersion as MemoryVersionRow,
    Space,
)
from memory.models import (
    MEMORY_EXTRACTION_STATUSES,
    MEMORY_KEY_VERSION,
    MEMORY_RELATION_TYPES,
    MEMORY_STATUSES,
    SOURCE_RELATIONS,
    ConsolidationRun,
    MemoryCandidate,
    MemoryDecision,
    MemoryExtractionState,
    MemoryRecord,
    MemoryRelation,
    MemorySource,
    MemoryVersion,
    memory_key_for,
    new_id,
    normalize_content,
    utc_now_iso,
)
from memory.retrieval_models import MemoryRetrievalHit
from memory.vector_lifecycle import (
    current_embedding_contract,
    memory_content_hash,
    memory_embedding_text,
)
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime


LOGGER = logging.getLogger(__name__)


def _schedule_memory_embedding(session: Any, row: Memory, *, force: bool = False) -> str | None:
    """函数功能：`_schedule_memory_embedding` 负责处理 schedule memory embedding，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        row: row 参数，由调用方传入，类型为 `Memory`。
        force: force 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    if not MEMORY_VECTOR_LIFECYCLE_ENABLED:
        return None
    model, dimension, version = current_embedding_contract()
    content_hash = memory_content_hash(
        memory_type=row.memory_type,
        subject=row.subject,
        predicate=row.predicate,
        object_value=row.object_value,
        content=row.content,
        model=model,
        dimension=dimension,
        embedding_version=version,
    )
    existing = session.get(MemoryVector, row.id)
    now = _dt(utc_now_iso())
    if (
        existing is not None
        and not force
        and existing.status == "ready"
        and existing.content_hash == content_hash
        and existing.model == model
        and existing.dimension == dimension
        and existing.embedding_version == version
        and existing.embedding is not None
    ):
        return None
    if existing is None:
        existing = MemoryVector(
            memory_id=row.id,
            embedding=None,
            model=model,
            dimension=dimension,
            content_hash=content_hash,
            embedding_version=version,
            status="pending",
            attempt_count=0,
            next_retry_at=None,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        session.add(existing)
    else:
        existing.embedding = None
        existing.model = model
        existing.dimension = dimension
        existing.content_hash = content_hash
        existing.embedding_version = version
        existing.status = "pending"
        existing.attempt_count = 0
        existing.next_retry_at = None
        existing.last_error = None
        existing.updated_at = now
    session.flush()
    from repositories.postgres.dispatch import _enqueue_task_in_session

    task_id, _ = _enqueue_task_in_session(
        session,
        task_type="memory_embedding",
        tenant_id=str(row.tenant_id),
        space_id=str(row.space_id),
        source_message_id=None,
        idempotency_key=f"memory_embedding:{row.id}:{content_hash}",
        payload={
            "operation": "memory_embedding",
            "memory_id": row.id,
            "content_hash": content_hash,
            "embedding_version": version,
        },
        priority=-1,
        max_attempts=MEMORY_VECTOR_MAX_ATTEMPTS,
        initial_status="queued",
        publish=True,
    )
    return task_id


def _schedule_memory_embedding_if_enabled(session: Any, row: Memory, *, force: bool = False) -> None:
    """函数功能：`_schedule_memory_embedding_if_enabled` 负责处理 schedule memory embedding if enabled，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        row: row 参数，由调用方传入，类型为 `Memory`。
        force: force 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    try:
        _schedule_memory_embedding(session, row, force=force)
    except Exception:
        LOGGER.exception("memory vector task scheduling failed memory_id=%s", row.id)
        raise


def _iso(value: Any) -> str | None:
    """函数功能：`_iso` 负责处理 iso，服务于本文件职责：Memory 数据访问。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _dt(value: Any) -> datetime | None:
    """函数功能：`_dt` 负责处理 dt，服务于本文件职责：Memory 数据访问。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
    返回结果说明：
        返回 `datetime | None`；未命中或无需处理时可返回 `None`。
    """
    if value is None:
        return None
    return parse_datetime(value)


def init_db(db_path: Any = None) -> None:
    """函数功能：`init_db` 负责处理 init db，服务于本文件职责：Memory 数据访问。
    传参：
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    del db_path


def _sources(session: Any, memory_id: str) -> list[MemorySource]:
    """函数功能：`_sources` 负责处理 sources，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
    返回结果说明：
        返回 `list[MemorySource]`，表示按条件筛选、构造或查询得到的列表。
    """
    rows = session.execute(
        select(MemorySourceRow).where(MemorySourceRow.memory_id == memory_id).order_by(MemorySourceRow.created_at)
    ).scalars()
    return [MemorySource(row.memory_id, row.note_id, row.relation, _iso(row.created_at) or "") for row in rows]


def _versions(session: Any, memory_id: str) -> list[MemoryVersion]:
    """函数功能：`_versions` 负责处理 versions，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
    返回结果说明：
        返回 `list[MemoryVersion]`，表示按条件筛选、构造或查询得到的列表。
    """
    rows = session.execute(
        select(MemoryVersionRow).where(MemoryVersionRow.memory_id == memory_id).order_by(MemoryVersionRow.version)
    ).scalars()
    return [
        MemoryVersion(
            id=row.id,
            memory_id=row.memory_id,
            version=row.version,
            content=row.content,
            status=row.status,
            reason=row.reason,
            source_note_id=row.source_note_id,
            created_at=_iso(row.created_at) or "",
            task_status=row.task_status,
            confidence=row.confidence,
            importance=row.importance,
            valid_from=_iso(row.valid_from),
            valid_until=_iso(row.valid_until),
        )
        for row in rows
    ]


def _source_map(session: Any, memory_ids: list[str]) -> dict[str, list[MemorySource]]:
    """函数功能：`_source_map` 负责处理 source map，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        memory_ids: memory ids 参数，由调用方传入，类型为 `list[str]`。
    返回结果说明：
        返回 `dict[str, list[MemorySource]]`，表示结构化结果、载荷或状态映射。
    """
    result: dict[str, list[MemorySource]] = {memory_id: [] for memory_id in memory_ids}
    if not memory_ids:
        return result
    rows = session.execute(
        select(MemorySourceRow)
        .where(MemorySourceRow.memory_id.in_(memory_ids))
        .order_by(MemorySourceRow.memory_id, MemorySourceRow.created_at)
    ).scalars()
    for row in rows:
        result[row.memory_id].append(MemorySource(row.memory_id, row.note_id, row.relation, _iso(row.created_at) or ""))
    return result


def _record(
    session: Any,
    row: Memory,
    *,
    include_versions: bool = True,
    sources: list[MemorySource] | None = None,
    versions: list[MemoryVersion] | None = None,
) -> MemoryRecord:
    """函数功能：`_record` 负责记录，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        row: row 参数，由调用方传入，类型为 `Memory`。
        include_versions: include versions 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        sources: sources 参数，由调用方传入，类型为 `list[MemorySource] | None`，默认值为 `None`。
        versions: versions 参数，由调用方传入，类型为 `list[MemoryVersion] | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return MemoryRecord(
        id=row.id,
        space_id=row.space_id,
        memory_type=row.memory_type,
        content=row.content,
        normalized_content=row.normalized_content or "",
        importance=float(row.importance),
        confidence=float(row.confidence),
        status=row.status,
        task_status=row.task_status,
        valid_from=_iso(row.valid_from),
        valid_until=_iso(row.valid_until),
        created_at=_iso(row.created_at) or "",
        updated_at=_iso(row.updated_at) or "",
        last_accessed_at=_iso(row.last_accessed_at),
        access_count=row.access_count,
        current_version=row.current_version,
        subject=row.subject,
        predicate=row.predicate,
        object_value=row.object_value,
        last_confirmed_at=_iso(row.last_confirmed_at),
        memory_key=row.memory_key,
        memory_key_version=row.memory_key_version or MEMORY_KEY_VERSION,
        polarity=row.polarity,
        scope=dict(row.scope_json or {}),
        sources=_sources(session, row.id) if sources is None else sources,
        versions=(_versions(session, row.id) if versions is None else versions) if include_versions else [],
    )


def _records(session: Any, rows: list[Memory], *, include_sources: bool = True) -> list[MemoryRecord]:
    """函数功能：`_records` 负责处理 records，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        rows: rows 参数，由调用方传入，类型为 `list[Memory]`。
        include_sources: include sources 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
    返回结果说明：
        返回 `list[MemoryRecord]`，表示按条件筛选、构造或查询得到的列表。
    """
    source_map = _source_map(session, [row.id for row in rows]) if include_sources else {}
    return [
        _record(
            session,
            row,
            include_versions=False,
            sources=source_map.get(row.id, []),
        )
        for row in rows
    ]


def _state(row: MemoryExtractionStateRow) -> MemoryExtractionState:
    """函数功能：`_state` 负责处理 state，服务于本文件职责：Memory 数据访问。
    传参：
        row: row 参数，由调用方传入，类型为 `MemoryExtractionStateRow`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return MemoryExtractionState(
        row.note_id, row.space_id, row.status, row.candidate_count, row.processed_count,
        row.attempt_count, row.last_error, _iso(row.started_at), _iso(row.completed_at), _iso(row.updated_at) or "",
    )


def _run(row: MemoryConsolidationRun) -> ConsolidationRun:
    """函数功能：`_run` 负责运行，服务于本文件职责：Memory 数据访问。
    传参：
        row: row 参数，由调用方传入，类型为 `MemoryConsolidationRun`。
    返回结果说明：
        返回 `ConsolidationRun` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return ConsolidationRun(
        row.id,
        row.space_id,
        row.cadence,
        row.period_key,
        row.status,
        _iso(row.started_at) or "",
        _iso(row.completed_at),
        row.error,
        json.dumps(row.result_json, ensure_ascii=False) if row.result_json is not None else None,
    )


def _add_source(session: Any, memory_id: str, note_id: str, relation: str, *, now: str | None = None) -> bool:
    """函数功能：`_add_source` 负责处理 add source，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        relation: relation 参数，由调用方传入，类型为 `str`。
        now: now 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if relation not in SOURCE_RELATIONS:
        raise ValueError(f"invalid source relation: {relation}")
    created = session.execute(
        insert(MemorySourceRow)
        .values(memory_id=memory_id, note_id=note_id, relation=relation, created_at=_dt(now or utc_now_iso()))
        .on_conflict_do_nothing()
        .returning(MemorySourceRow.memory_id)
    ).scalar_one_or_none()
    return created is not None


def _add_version(session: Any, row: Memory, *, reason: str | None, source_note_id: str | None) -> None:
    """函数功能：`_add_version` 负责处理 add version，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        row: row 参数，由调用方传入，类型为 `Memory`。
        reason: reason 参数，由调用方传入，类型为 `str | None`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str | None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    session.add(
        MemoryVersionRow(
            id=new_id("ver"),
            memory_id=row.id,
            version=row.current_version,
            content=row.content,
            status=row.status,
            task_status=row.task_status,
            confidence=row.confidence,
            importance=row.importance,
            valid_from=_dt(row.valid_from),
            valid_until=_dt(row.valid_until),
            reason=reason,
            source_note_id=source_note_id,
            created_at=_dt(utc_now_iso()),
        )
    )


def _insert_memory(
    session: Any,
    space_id: str,
    candidate: MemoryCandidate,
    *,
    source_note_id: str,
    source_relation: str = "created_from",
    status: str = "active",
    memory_id: str | None = None,
    now: str | None = None,
) -> Memory:
    """函数功能：`_insert_memory` 负责处理 insert memory，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str`。
        source_relation: source relation 参数，由调用方传入，类型为 `str`，默认值为 `'created_from'`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'active'`。
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str | None`，默认值为 `None`。
        now: now 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `Memory` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if status not in MEMORY_STATUSES:
        raise ValueError(f"invalid memory status: {status}")
    space = session.get(Space, space_id)
    tenant_id = str(space.tenant_id) if space is not None else DEFAULT_TENANT_ID
    space_id = ensure_tenant_space(session, space_id, tenant_id=tenant_id)
    timestamp = now or utc_now_iso()
    row = Memory(
        id=memory_id or new_id("mem"),
        tenant_id=tenant_id,
        space_id=space_id,
        memory_type=candidate.memory_type,
        content=candidate.content,
        normalized_content=candidate.normalized_content,
        importance=float(candidate.importance),
        confidence=float(candidate.confidence),
        status=status,
        task_status=candidate.task_status,
        subject=candidate.subject,
        predicate=candidate.predicate,
        object_value=candidate.object_value,
        memory_key=candidate.effective_memory_key,
        memory_key_version=candidate.memory_key_version,
        polarity=candidate.polarity,
        scope_json=dict(candidate.scope),
        valid_from=_dt(candidate.valid_from or timestamp),
        valid_until=_dt(candidate.valid_until),
        last_confirmed_at=_dt(timestamp),
        created_at=_dt(timestamp),
        updated_at=_dt(timestamp),
        current_version=1,
    )
    session.add(row)
    session.flush()
    _add_source(session, row.id, source_note_id, source_relation, now=timestamp)
    _add_version(session, row, reason=candidate.effective_reason or "memory_created", source_note_id=source_note_id)
    _schedule_memory_embedding_if_enabled(session, row)
    return row


def _versioned_update(
    session: Any,
    row: Memory,
    *,
    content: str | None = None,
    status: str | None = None,
    task_status: str | None = None,
    valid_until: str | None = None,
    confidence: float | None = None,
    importance: float | None = None,
    last_confirmed_at: str | None = None,
    object_value: str | None = None,
    scope: dict[str, Any] | None = None,
    memory_key: str | None = None,
    memory_key_version: str | None = None,
    reason: str | None,
    source_note_id: str | None,
) -> None:
    """函数功能：`_versioned_update` 负责更新 versioned，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        row: row 参数，由调用方传入，类型为 `Memory`。
        content: 需要处理、保存或展示的文本内容，类型为 `str | None`，默认值为 `None`。
        status: status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        task_status: task status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        valid_until: valid until 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        confidence: confidence 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        importance: importance 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        last_confirmed_at: last confirmed at 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        object_value: object value 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        scope: scope 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
        memory_key: memory key 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        memory_key_version: memory key version 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        reason: reason 参数，由调用方传入，类型为 `str | None`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str | None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    if status is not None and status not in MEMORY_STATUSES:
        raise ValueError(f"invalid memory status: {status}")
    content_changed = content is not None
    if content_changed:
        row.content = content
        row.normalized_content = normalize_content(content)
        if row.memory_key_version != "memory-key-v3":
            row.memory_key = memory_key_for(
                row.memory_type,
                subject=row.subject,
                predicate=row.predicate,
                object_value=row.object_value,
                content=content,
            )
    if status is not None:
        row.status = status
    if task_status is not None:
        row.task_status = task_status
    if valid_until is not None:
        row.valid_until = _dt(valid_until)
    if confidence is not None:
        row.confidence = float(confidence)
    if importance is not None:
        row.importance = float(importance)
    if last_confirmed_at is not None:
        row.last_confirmed_at = _dt(last_confirmed_at)
    if object_value is not None:
        row.object_value = object_value
    if scope is not None:
        row.scope_json = dict(scope)
    if memory_key is not None:
        row.memory_key = memory_key
    if memory_key_version is not None:
        row.memory_key_version = memory_key_version
    row.updated_at = _dt(utc_now_iso())
    row.current_version += 1
    session.flush()
    _add_version(session, row, reason=reason, source_note_id=source_note_id)
    if content_changed:
        _schedule_memory_embedding_if_enabled(session, row)


def _archive_terminal_task_duplicates(
    session: Any,
    target: Memory,
    candidate: MemoryCandidate,
    *,
    decision_id: str,
    source_note_id: str,
    now: str,
) -> list[str]:
    """函数功能：`_archive_terminal_task_duplicates` 负责归档 terminal task duplicates，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        target: target 参数，由调用方传入，类型为 `Memory`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        decision_id: decision id 参数，由调用方传入，类型为 `str`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str`。
        now: now 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    if target.memory_type != "task" or target.task_status not in {"done", "cancelled"}:
        return []
    from memory.canonicalizer import task_identity_compatible

    rows = list(
        session.execute(
            select(Memory)
            .where(Memory.space_id == target.space_id, Memory.memory_type == "task", Memory.status == "active", Memory.id != target.id)
            .with_for_update()
        ).scalars()
    )
    archived: list[str] = []
    for duplicate in rows:
        if not task_identity_compatible(candidate, duplicate):
            continue
        _versioned_update(
            session,
            duplicate,
            status="archived",
            reason="duplicate_task_identity_reconciled",
            source_note_id=source_note_id,
        )
        _add_relation(session, target.space_id, target.id, duplicate.id, "supersedes", decision_id, now)
        _add_relation(session, target.space_id, duplicate.id, target.id, "superseded_by", decision_id, now)
        archived.append(duplicate.id)
    return archived


def _add_relation(session: Any, space_id: str, source_id: str, target_id: str, relation: str, decision_id: str | None, now: str) -> None:
    """函数功能：`_add_relation` 负责处理 add relation，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        source_id: source id 参数，由调用方传入，类型为 `str`。
        target_id: target id 参数，由调用方传入，类型为 `str`。
        relation: relation 参数，由调用方传入，类型为 `str`。
        decision_id: decision id 参数，由调用方传入，类型为 `str | None`。
        now: now 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    if relation not in MEMORY_RELATION_TYPES:
        raise ValueError(f"invalid memory relation: {relation}")
    session.execute(
        insert(MemoryRelationRow)
        .values(
            id=new_id("rel"), space_id=space_id, source_memory_id=source_id,
            target_memory_id=target_id, relation=relation, decision_id=decision_id, created_at=_dt(now),
        )
        .on_conflict_do_nothing()
    )


def _save_decision(
    session: Any,
    space_id: str,
    note_id: str,
    decision: MemoryDecision,
    *,
    status: str,
    result_ids: list[str] | None = None,
    error: str | None = None,
) -> None:
    """函数功能：`_save_decision` 负责保存 decision，服务于本文件职责：Memory 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        decision: decision 参数，由调用方传入，类型为 `MemoryDecision`。
        status: status 参数，由调用方传入，类型为 `str`。
        result_ids: result ids 参数，由调用方传入，类型为 `list[str] | None`，默认值为 `None`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    now = utc_now_iso()
    session.execute(
        insert(MemoryDecisionRow)
        .values(
            id=decision.decision_id,
            space_id=space_id,
            note_id=note_id,
            candidate_id=decision.candidate_id,
            relation=decision.relation,
            target_memory_ids_json=list(decision.target_memory_ids),
            confidence=float(decision.confidence),
            reason=decision.reason,
            evidence_json=list(decision.evidence),
            recommended_action=decision.recommended_action,
            status=status,
            result_memory_ids_json=list(result_ids or []),
            error=error,
            policy_version=decision.policy_version,
            adjudicator_version=decision.adjudicator_version,
            model=decision.model,
            prompt_hash=decision.prompt_hash,
            input_hash=decision.input_hash,
            target_snapshot_version=decision.target_snapshot_version,
            retry_of_decision_id=decision.retry_of_decision_id,
            created_at=_dt(now),
            applied_at=_dt(now) if status == "applied" else None,
        )
        .on_conflict_do_update(
            index_elements=[MemoryDecisionRow.id],
            set_={"status": status, "result_memory_ids_json": list(result_ids or []), "error": error},
        )
    )


def add_source(memory_id: str, note_id: str, relation: str, db_path: Any = None) -> bool:
    """函数功能：`add_source` 负责处理 add source，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        relation: relation 参数，由调用方传入，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    del db_path
    with session_scope() as session:
        row = session.execute(select(Memory).where(Memory.id == memory_id).with_for_update()).scalar_one_or_none()
        return False if row is None else _add_source(session, memory_id, note_id, relation)


def insert_memory(space_id: str, candidate: MemoryCandidate, *, source_note_id: str, source_relation: str = "created_from", status: str = "active", db_path: Any = None) -> MemoryRecord:
    """函数功能：`insert_memory` 负责处理 insert memory，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str`。
        source_relation: source relation 参数，由调用方传入，类型为 `str`，默认值为 `'created_from'`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'active'`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    del db_path
    with session_scope() as session:
        row = _insert_memory(session, space_id, candidate, source_note_id=source_note_id, source_relation=source_relation, status=status)
        session.flush()
        result = _record(session, row)
    return result


def get_memory(memory_id: str, db_path: Any = None) -> MemoryRecord | None:
    """函数功能：`get_memory` 负责获取 memory，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    with session_scope() as session:
        row = session.get(Memory, memory_id)
        return _record(session, row) if row is not None else None


def list_memories(
    space_id: str,
    *,
    status: str | None = "active",
    memory_type: str | None = None,
    memory_key: str | None = None,
    include_expired: bool = False,
    limit: int = 20,
    db_path: Any = None,
) -> list[MemoryRecord]:
    """函数功能：`list_memories` 负责列出 memories，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str | None`，默认值为 `'active'`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        memory_key: memory key 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        include_expired: include expired 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `20`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRecord]`，表示按条件筛选、构造或查询得到的列表。
    """
    del db_path
    statement = select(Memory).where(Memory.space_id == space_id)
    if status:
        statement = statement.where(Memory.status == status)
    if memory_type:
        statement = statement.where(Memory.memory_type == memory_type)
    if memory_key:
        statement = statement.where(Memory.memory_key == memory_key)
    if status == "active" and not include_expired:
        statement = statement.where((Memory.valid_until.is_(None)) | (Memory.valid_until > _dt(utc_now_iso())))
    statement = statement.order_by(Memory.updated_at.desc(), Memory.id.desc()).limit(max(1, min(int(limit), 100)))
    with session_scope() as session:
        rows = list(session.execute(statement).scalars())
        return _records(session, rows)


def list_adjudication_candidates(
    space_id: str,
    *,
    memory_type: str,
    memory_key: str,
    limit: int = 200,
    db_path: Any = None,
) -> list[MemoryRecord]:
    """函数功能：`list_adjudication_candidates` 负责列出 adjudication candidates，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str`。
        memory_key: memory key 参数，由调用方传入，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `200`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRecord]`，表示按条件筛选、构造或查询得到的列表。
    """
    del db_path
    statement = (
        select(Memory)
        .where(
            Memory.space_id == space_id,
            Memory.status == "active",
            Memory.memory_type == memory_type,
            (Memory.valid_until.is_(None)) | (Memory.valid_until > _dt(utc_now_iso())),
        )
        .order_by(
            case((Memory.memory_key == memory_key, 0), else_=1),
            Memory.updated_at.desc(),
            Memory.id.desc(),
        )
        .limit(max(1, min(int(limit), 500)))
    )
    with session_scope() as session:
        rows = list(session.execute(statement).scalars())
        return _records(session, rows, include_sources=False)


def _base_memory_statement(
    space_id: str,
    *,
    memory_type: str | None,
    include_inactive: bool = False,
) -> Any:
    """函数功能：`_base_memory_statement` 负责处理 base memory statement，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`。
        include_inactive: include inactive 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
    返回结果说明：
        返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    statement = select(Memory).where(Memory.space_id == space_id)
    if memory_type:
        statement = statement.where(Memory.memory_type == memory_type)
    if include_inactive:
        statement = statement.where(Memory.status != "forgotten")
    else:
        statement = statement.where(
            Memory.status == "active",
            (Memory.valid_until.is_(None)) | (Memory.valid_until > _dt(utc_now_iso())),
        )
    return statement


def _text_document() -> Any:
    """函数功能：`_text_document` 负责处理 text document，服务于本文件职责：Memory 数据访问。
    传参：
        无。
    返回结果说明：
        返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if hasattr(Memory, "search_document"):
        return Memory.search_document
    return func.to_tsvector(
        "simple",
        func.concat_ws(
            " ",
            func.coalesce(Memory.content, ""),
            func.coalesce(Memory.subject, ""),
            func.coalesce(Memory.predicate, ""),
            func.coalesce(Memory.object_value, ""),
        ),
    )


def _query_terms(text: str, *, entities: list[str] | None = None) -> list[str]:
    # 先放入去除意图词后的主题，避免有限 term expansion 在抵达真实实体前被通用问句 n-gram 消耗完。
    """函数功能：`_query_terms` 负责查询 terms，服务于本文件职责：Memory 数据访问。
    传参：
        text: 输入文本内容，类型为 `str`。
        entities: entities 参数，由调用方传入，类型为 `list[str] | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    from memory.retriever import retrieval_topic_text

    topic = retrieval_topic_text(text)
    values = [topic, str(text or "")] if topic and topic != str(text or "") else [str(text or "")]
    values.extend(str(item or "") for item in entities or [])
    terms: list[str] = []

    def add(value: str) -> None:
        """函数功能：`add` 负责处理 add，服务于本文件职责：Memory 数据访问。
        传参：
            value: 待转换、校验或计算的值，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        token = str(value or "").strip()
        if len(token) >= 2 and token not in terms:
            terms.append(token)

    for value in values:
        for token in value.replace("：", " ").replace(":", " ").split():
            add(token)

        # PostgreSQL simple full-text parser 不切中文，用户也常省略连接词或调整 ASCII 名称周围空白。
        # 这里补充有界的中英文片段，让结构化词法通道仍能召回给 scorer；这些只是检索提示，不改变 memory identity，也不允许写侧合并。
        runs = re.findall(r"[A-Za-z0-9][A-Za-z0-9+#._-]*|[\u4e00-\u9fff]+", value)
        for run in runs:
            add(run)
        for run in runs:
            if not re.fullmatch(r"[\u4e00-\u9fff]+", run):
                continue
            for width in (4, 3, 2):
                if len(run) < width:
                    continue
                for start in range(len(run) - width + 1):
                    add(run[start : start + width])

    compact = normalize_content(text)
    add(compact)
    return terms[:12]


def _rrf_hits(
    channels: list[tuple[str, list[Memory]]],
    *,
    exact_key: str | None = None,
    subject: str | None = None,
    predicate: str | None = None,
    limit: int = 20,
) -> list[MemoryRetrievalHit]:
    """函数功能：`_rrf_hits` 负责处理 rrf hits，服务于本文件职责：Memory 数据访问。
    传参：
        channels: channels 参数，由调用方传入，类型为 `list[tuple[str, list[Memory]]]`。
        exact_key: exact key 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        subject: subject 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        predicate: predicate 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `20`。
    返回结果说明：
        返回 `list[MemoryRetrievalHit]`，表示按条件筛选、构造或查询得到的列表。
    """
    scores: dict[str, float] = {}
    rows: dict[str, Memory] = {}
    ranks: dict[str, dict[str, int]] = {}
    rrf_k = max(1, int(MEMORY_HYBRID_RRF_K))
    channel_weights = {
        # 结构化/canonical 命中对状态层查询信号最强；稀疏和向量通道用于补充，trigram 主要用于错字/分词恢复，因此权重更弱。
        "exact": 1.60,
        "structured": 1.35,
        "fts": 1.00,
        "vector": 0.95,
        "trigram": 0.60,
    }
    for channel, ranked in channels:
        weight = channel_weights.get(channel, 1.0) if RETRIEVAL_WEIGHTED_RRF_ENABLED else 1.0
        for rank, row in enumerate(ranked, start=1):
            rows[row.id] = row
            ranks.setdefault(row.id, {})[channel] = rank
            scores[row.id] = scores.get(row.id, 0.0) + weight / (rrf_k + rank)
    hits: list[MemoryRetrievalHit] = []
    for memory_id, row in rows.items():
        policy_score = 0.0
        reasons = []
        if exact_key and row.memory_key == exact_key:
            scores[memory_id] += 0.08
            policy_score += 0.08
            reasons.append("exact_key_boost")
        if subject and row.subject and normalize_content(subject) == normalize_content(row.subject):
            scores[memory_id] += 0.03
            policy_score += 0.03
            reasons.append("subject_match")
        if predicate and row.predicate and normalize_content(predicate) == normalize_content(row.predicate):
            scores[memory_id] += 0.03
            policy_score += 0.03
            reasons.append("predicate_match")
        channel_ranks = ranks.get(memory_id, {})
        hits.append(
            MemoryRetrievalHit(
                memory=_record(_NoSourceSession(), row, include_versions=False, sources=[]),
                exact_rank=channel_ranks.get("exact"),
                structured_rank=channel_ranks.get("structured"),
                fts_rank=channel_ranks.get("fts"),
                trigram_rank=channel_ranks.get("trigram"),
                vector_rank=channel_ranks.get("vector"),
                exact_score=(channel_weights["exact"] if RETRIEVAL_WEIGHTED_RRF_ENABLED else 1.0) / (rrf_k + channel_ranks["exact"]) if "exact" in channel_ranks else 0.0,
                structured_score=(channel_weights["structured"] if RETRIEVAL_WEIGHTED_RRF_ENABLED else 1.0) / (rrf_k + channel_ranks["structured"]) if "structured" in channel_ranks else 0.0,
                fts_score=(channel_weights["fts"] if RETRIEVAL_WEIGHTED_RRF_ENABLED else 1.0) / (rrf_k + channel_ranks["fts"]) if "fts" in channel_ranks else 0.0,
                trigram_score=(channel_weights["trigram"] if RETRIEVAL_WEIGHTED_RRF_ENABLED else 1.0) / (rrf_k + channel_ranks["trigram"]) if "trigram" in channel_ranks else 0.0,
                vector_score=(channel_weights["vector"] if RETRIEVAL_WEIGHTED_RRF_ENABLED else 1.0) / (rrf_k + channel_ranks["vector"]) if "vector" in channel_ranks else 0.0,
                rrf_score=scores.get(memory_id, 0.0) - policy_score,
                policy_score=policy_score,
                final_score=scores.get(memory_id, 0.0),
                reasons=reasons + sorted(channel_ranks),
            )
        )
    hits.sort(key=lambda hit: (hit.final_score, hit.memory.updated_at, hit.memory.id), reverse=True)
    return hits[: max(1, min(int(limit), 50))]


class _NoSourceSession:
    """类功能：`_NoSourceSession` 封装与“Memory 数据访问”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        """函数功能：`_NoSourceSession.execute` 在类 `_NoSourceSession` 中负责执行，服务于本文件职责：Memory 数据访问。
        传参：
            *_args:  args 参数，由调用方传入，类型为 `Any`。
            **_kwargs:  kwargs 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        raise RuntimeError("sources unavailable in lightweight retrieval hit")


def _rrf_fuse(*args: Any, **kwargs: Any) -> list[Memory]:
    """函数功能：`_rrf_fuse` 负责处理 rrf fuse，服务于本文件职责：Memory 数据访问。
    传参：
        *args: args 参数，由调用方传入，类型为 `Any`。
        **kwargs: kwargs 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `list[Memory]`，表示按条件筛选、构造或查询得到的列表。
    """
    if args and args[0] and isinstance(args[0][0], list):
        channels = [(f"channel_{index}", ranked) for index, ranked in enumerate(args[0])]
        args = (channels, *args[1:])
    return [hit.memory for hit in _rrf_hits(*args, **kwargs)]


def _ready_vector_count(space_id: str, memory_type: str | None = None) -> int:
    """函数功能：`_ready_vector_count` 负责计数 ready vector，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    with session_scope() as session:
        statement = (
            select(func.count())
            .select_from(MemoryVector)
            .join(Memory, Memory.id == MemoryVector.memory_id)
            .where(
                Memory.space_id == space_id,
                Memory.status == "active",
                MemoryVector.status == "ready",
                MemoryVector.embedding.is_not(None),
            )
        )
        if memory_type:
            statement = statement.where(Memory.memory_type == memory_type)
        return int(session.execute(statement).scalar_one() or 0)


def _safe_embedding(space_id: str, text: str, *, memory_type: str | None = None) -> list[float] | None:
    """函数功能：`_safe_embedding` 负责处理 safe embedding，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        text: 输入文本内容，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[float] | None`，表示按条件筛选、构造或查询得到的列表。
    """
    if not MEMORY_HYBRID_VECTOR_ENABLED or FAKE_EXTERNALS or not text.strip():
        return None
    try:
        if _ready_vector_count(space_id, memory_type=memory_type) <= 0:
            return None
        from core.llm_client import embed_text

        embedding = embed_text(text)
        _, dimension, _ = current_embedding_contract()
        return embedding if len(embedding) == dimension else None
    except Exception as exc:
        LOGGER.debug("memory hybrid vector retrieval disabled for request: %s", type(exc).__name__)
        return None


def claim_memory_vector(memory_id: str, expected_hash: str | None = None) -> dict[str, Any] | None:
    """函数功能：`claim_memory_vector` 负责认领 memory vector，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        expected_hash: expected hash 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    if not MEMORY_VECTOR_LIFECYCLE_ENABLED:
        return None
    model, dimension, version = current_embedding_contract()
    with session_scope() as session:
        row = session.execute(
            select(Memory).where(Memory.id == memory_id, Memory.status == "active").with_for_update()
        ).scalar_one_or_none()
        if row is None:
            return None
        content_hash = memory_content_hash(
            memory_type=row.memory_type,
            subject=row.subject,
            predicate=row.predicate,
            object_value=row.object_value,
            content=row.content,
            model=model,
            dimension=dimension,
            embedding_version=version,
        )
        if expected_hash and expected_hash != content_hash:
            return None
        vector = session.execute(
            select(MemoryVector).where(MemoryVector.memory_id == memory_id).with_for_update()
        ).scalar_one_or_none()
        if vector is None:
            vector = MemoryVector(
                memory_id=memory_id,
                embedding=None,
                model=model,
                dimension=dimension,
                content_hash=content_hash,
                embedding_version=version,
                status="pending",
                attempt_count=0,
                next_retry_at=None,
                last_error=None,
                created_at=_dt(utc_now_iso()),
                updated_at=_dt(utc_now_iso()),
            )
            session.add(vector)
            session.flush()
        if (
            vector.status == "ready"
            and vector.content_hash == content_hash
            and vector.model == model
            and vector.dimension == dimension
            and vector.embedding_version == version
            and vector.embedding is not None
        ):
            return None
        if vector.status == "processing" and vector.content_hash == content_hash:
            return None
        vector.status = "processing"
        vector.content_hash = content_hash
        vector.model = model
        vector.dimension = dimension
        vector.embedding_version = version
        vector.attempt_count = int(vector.attempt_count or 0) + 1
        vector.next_retry_at = None
        vector.last_error = None
        vector.updated_at = _dt(utc_now_iso())
        session.flush()
        return {
            "memory_id": memory_id,
            "text": memory_embedding_text(
                memory_type=row.memory_type,
                subject=row.subject,
                predicate=row.predicate,
                object_value=row.object_value,
                content=row.content,
            ),
            "content_hash": content_hash,
            "model": model,
            "dimension": dimension,
            "embedding_version": version,
            "attempt_count": int(vector.attempt_count),
        }


def complete_memory_vector(
    memory_id: str,
    *,
    content_hash: str,
    embedding: list[float],
    model: str,
    dimension: int,
    embedding_version: str,
) -> bool:
    """函数功能：`complete_memory_vector` 负责完成 memory vector，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content_hash: content hash 参数，由调用方传入，类型为 `str`。
        embedding: embedding 参数，由调用方传入，类型为 `list[float]`。
        model: model 参数，由调用方传入，类型为 `str`。
        dimension: dimension 参数，由调用方传入，类型为 `int`。
        embedding_version: embedding version 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if len(embedding) != int(dimension):
        raise ValueError(f"memory embedding dimension mismatch: expected {dimension}, got {len(embedding)}")
    with session_scope() as session:
        result = session.execute(
            update(MemoryVector)
            .where(
                MemoryVector.memory_id == memory_id,
                MemoryVector.status == "processing",
                MemoryVector.content_hash == content_hash,
            )
            .values(
                embedding=[float(value) for value in embedding],
                model=model,
                dimension=int(dimension),
                embedding_version=embedding_version,
                status="ready",
                last_error=None,
                next_retry_at=None,
                updated_at=_dt(utc_now_iso()),
            )
            .returning(MemoryVector.memory_id)
        ).scalar_one_or_none()
        return result is not None


def fail_memory_vector(memory_id: str, *, content_hash: str, error: str) -> bool:
    """函数功能：`fail_memory_vector` 负责处理 fail memory vector，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content_hash: content hash 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    with session_scope() as session:
        row = session.execute(
            select(MemoryVector)
            .where(MemoryVector.memory_id == memory_id, MemoryVector.content_hash == content_hash)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            return False
        delay = min(300.0, MEMORY_VECTOR_RETRY_BASE_SECONDS * (2 ** max(0, int(row.attempt_count or 1) - 1)))
        row.status = "failed"
        row.last_error = error[:2000]
        row.next_retry_at = datetime.now().astimezone() + timedelta(seconds=delay)
        row.updated_at = _dt(utc_now_iso())
        return True


def list_memory_vector_backfill_candidates(*, status: str = "active", limit: int = 10000) -> list[dict[str, Any]]:
    """函数功能：`list_memory_vector_backfill_candidates` 负责列出 memory vector backfill candidates，服务于本文件职责：Memory 数据访问。
    传参：
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'active'`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `10000`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    model, dimension, version = current_embedding_contract()
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Memory)
                .where(Memory.status == status)
                .order_by(Memory.updated_at, Memory.id)
                .limit(max(1, min(int(limit), 100000)))
            ).scalars()
        )
        result = []
        for row in rows:
            content_hash = memory_content_hash(
                memory_type=row.memory_type,
                subject=row.subject,
                predicate=row.predicate,
                object_value=row.object_value,
                content=row.content,
                model=model,
                dimension=dimension,
                embedding_version=version,
            )
            vector = session.get(MemoryVector, row.id)
            if (
                vector is not None
                and vector.status == "ready"
                and vector.content_hash == content_hash
                and vector.model == model
                and vector.dimension == dimension
                and vector.embedding_version == version
                and vector.embedding is not None
            ):
                continue
            result.append({"memory_id": row.id, "space_id": row.space_id, "content_hash": content_hash})
        return result


def schedule_memory_vector_backfill(*, status: str = "active", limit: int = 10000) -> int:
    """函数功能：`schedule_memory_vector_backfill` 负责回填 schedule memory vector，服务于本文件职责：Memory 数据访问。
    传参：
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'active'`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `10000`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    if not MEMORY_VECTOR_LIFECYCLE_ENABLED:
        return 0
    model, dimension, version = current_embedding_contract()
    scheduled = 0
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Memory)
                .where(Memory.status == status)
                .order_by(Memory.updated_at, Memory.id)
                .limit(max(1, min(int(limit), 100000)))
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        for row in rows:
            content_hash = memory_content_hash(
                memory_type=row.memory_type,
                subject=row.subject,
                predicate=row.predicate,
                object_value=row.object_value,
                content=row.content,
                model=model,
                dimension=dimension,
                embedding_version=version,
            )
            vector = session.get(MemoryVector, row.id)
            if (
                vector is not None
                and vector.status in {"pending", "processing"}
                and vector.content_hash == content_hash
            ):
                continue
            if (
                vector is not None
                and vector.status == "ready"
                and vector.content_hash == content_hash
                and vector.model == model
                and vector.dimension == dimension
                and vector.embedding_version == version
                and vector.embedding is not None
            ):
                continue
            if _schedule_memory_embedding(session, row) is not None:
                scheduled += 1
    return scheduled


def hybrid_adjudication_candidates(
    space_id: str,
    candidate: MemoryCandidate,
    *,
    query_embedding: list[float] | None = None,
    limit: int = 20,
    db_path: Any = None,
) -> list[MemoryRecord]:
    """函数功能：`hybrid_adjudication_candidates` 负责处理 hybrid adjudication candidates，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        query_embedding: query embedding 参数，由调用方传入，类型为 `list[float] | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `20`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRecord]`，表示按条件筛选、构造或查询得到的列表。
    """
    del db_path
    memory_type = candidate.memory_type
    top = max(1, min(int(limit), 50))
    retrieval_limit = max(30, top * 3)
    embedding = query_embedding or _safe_embedding(space_id, candidate.content, memory_type=memory_type)
    terms = _query_terms(candidate.content, entities=candidate.entities)

    channels: list[tuple[str, list[Memory]]] = []
    _, embedding_dimension, embedding_version = current_embedding_contract()
    with session_scope() as session:
        base = _base_memory_statement(space_id, memory_type=memory_type)
        if candidate.effective_memory_key:
            channels.append((
                "exact",
                list(
                    session.execute(
                        base.where(Memory.memory_key == candidate.effective_memory_key)
                        .order_by(Memory.updated_at.desc(), Memory.id.desc())
                        .limit(20)
                    ).scalars()
                )
            ))

        structured_filters = []
        if candidate.memory_type and candidate.predicate:
            structured_filters.append((Memory.memory_type == candidate.memory_type) & (func.lower(Memory.predicate) == candidate.predicate.casefold()))
        if candidate.subject and candidate.predicate:
            structured_filters.append((func.lower(Memory.subject) == candidate.subject.casefold()) & (func.lower(Memory.predicate) == candidate.predicate.casefold()))
        if candidate.predicate and candidate.object_value:
            structured_filters.append((func.lower(Memory.predicate) == candidate.predicate.casefold()) & Memory.object_value.ilike(f"%{candidate.object_value[:120]}%"))
        if candidate.object_value:
            structured_filters.append(Memory.object_value.ilike(f"%{candidate.object_value[:120]}%"))
        for entity in candidate.entities[:5]:
            structured_filters.append((Memory.memory_type == candidate.memory_type) & Memory.content.ilike(f"%{str(entity)[:120]}%"))
        if structured_filters:
            channels.append((
                "structured",
                list(
                    session.execute(
                        base.where(or_(*structured_filters))
                        .order_by(
                            case((Memory.memory_key == candidate.effective_memory_key, 0), else_=1),
                            Memory.updated_at.desc(),
                            Memory.id.desc(),
                        )
                        .limit(retrieval_limit)
                    ).scalars()
                )
            ))
        if candidate.memory_type == "task":
            # 旧版 V2 任务可能使用泛化 subject/predicate 槽位，因此为 identity bridge 保留一个有界任务切片。
            channels.append((
                "structured",
                list(
                    session.execute(
                        base.order_by(Memory.updated_at.desc(), Memory.id.desc()).limit(retrieval_limit)
                    ).scalars()
                ),
            ))

        query_text = " ".join(terms) or candidate.content
        if query_text.strip():
            document = _text_document()
            tsquery = func.plainto_tsquery("simple", query_text[:500])
            channels.append((
                "fts",
                list(
                    session.execute(
                        base.where(document.op("@@")(tsquery))
                        .order_by(func.ts_rank_cd(document, tsquery).desc(), Memory.updated_at.desc())
                        .limit(retrieval_limit)
                    ).scalars()
                )
            ))
            lexical_filters = [Memory.content.ilike(f"%{term[:120]}%") for term in terms[:8]]
            if lexical_filters:
                channels.append((
                    "structured",
                    list(
                        session.execute(
                            base.where(or_(*lexical_filters))
                            .order_by(Memory.updated_at.desc(), Memory.id.desc())
                            .limit(retrieval_limit)
                        ).scalars()
                    )
                ))
            if MEMORY_TRIGRAM_ENABLED:
                trigram_filters = [Memory.content.op("%")(term[:120]) for term in terms[:8]]
                trigram_filters.extend(Memory.object_value.op("%")(term[:120]) for term in terms[:8])
                channels.append((
                    "trigram",
                    list(
                        session.execute(
                            base.where(or_(*trigram_filters))
                            .order_by(
                                func.greatest(
                                    *[func.similarity(Memory.content, term[:120]) for term in terms[:8]],
                                    *[func.similarity(func.coalesce(Memory.object_value, ""), term[:120]) for term in terms[:8]],
                                ).desc(),
                                Memory.updated_at.desc(),
                            )
                            .limit(retrieval_limit)
                        ).scalars()
                    ),
                ))

        if embedding and len(embedding) == embedding_dimension:
            model, dimension, version = current_embedding_contract()
            channels.append((
                "vector",
                list(
                    session.execute(
                        base.join(MemoryVector, MemoryVector.memory_id == Memory.id)
                        .where(
                            MemoryVector.status == "ready",
                            MemoryVector.embedding.is_not(None),
                            MemoryVector.model == model,
                            MemoryVector.dimension == dimension,
                            MemoryVector.embedding_version == version,
                        )
                        .order_by(MemoryVector.embedding.cosine_distance(embedding), Memory.updated_at.desc())
                        .limit(retrieval_limit)
                    ).scalars()
                )
            ))

        hits = _rrf_hits(
            channels,
            exact_key=candidate.effective_memory_key,
            subject=candidate.subject,
            predicate=candidate.predicate,
            limit=top,
        )
        return [hit.memory for hit in hits]


def expire_due_memories(space_id: str | None = None, *, limit: int = 500, db_path: Any = None) -> int:
    """函数功能：`expire_due_memories` 负责处理过期状态 due memories，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `500`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    del db_path
    with session_scope() as session:
        statement = select(Memory).where(
            Memory.status == "active",
            Memory.valid_until.is_not(None),
            Memory.valid_until <= _dt(utc_now_iso()),
        )
        if space_id:
            statement = statement.where(Memory.space_id == space_id)
        statement = statement.order_by(Memory.valid_until).limit(max(1, min(int(limit), 1000))).with_for_update(skip_locked=True)
        rows = list(session.execute(statement).scalars())
        for row in rows:
            _versioned_update(
                session,
                row,
                status="expired",
                valid_until=row.valid_until,
                reason="valid_until_reached",
                source_note_id=None,
            )
        return len(rows)


def _candidate_record(row: MemoryCandidateRow) -> MemoryCandidate:
    """函数功能：`_candidate_record` 负责记录 candidate，服务于本文件职责：Memory 数据访问。
    传参：
        row: row 参数，由调用方传入，类型为 `MemoryCandidateRow`。
    返回结果说明：
        返回 `MemoryCandidate` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return MemoryCandidate(
        memory_type=row.memory_type,
        content=row.content,
        importance=float(row.importance),
        confidence=float(row.confidence),
        entities=list(row.entities_json or []),
        should_store=bool(row.should_store),
        task_status=row.task_status,
        candidate_id=row.candidate_id,
        note_id=row.note_id,
        space_id=row.space_id,
        subject=row.subject,
        predicate=row.predicate,
        object_value=row.object_value,
        valid_from=_iso(row.valid_from),
        valid_until=_iso(row.valid_until),
        evidence_span=row.evidence_span,
        clause_index=row.clause_index,
        memory_key=row.memory_key,
        memory_key_version=row.memory_key_version or MEMORY_KEY_VERSION,
        polarity=row.polarity,
        scope=dict(row.scope_json or {}),
        extractor_type=row.extractor_type,
        extractor_version=row.extractor_version,
        model=row.model,
        prompt_hash=row.prompt_hash,
    )


def save_memory_candidate(
    candidate: MemoryCandidate,
    *,
    space_id: str,
    status: str = "extracted",
    error: str | None = None,
    decision_id: str | None = None,
    db_path: Any = None,
) -> MemoryCandidate:
    """函数功能：`save_memory_candidate` 负责保存 memory candidate，服务于本文件职责：Memory 数据访问。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'extracted'`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
        decision_id: decision id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryCandidate` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    del db_path
    with session_scope() as session:
        space = session.get(Space, space_id)
        tenant_id = str(space.tenant_id) if space is not None else DEFAULT_TENANT_ID
        space_id = ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        values = {
            "candidate_id": candidate.candidate_id,
            "tenant_id": tenant_id,
            "space_id": space_id,
            "note_id": candidate.note_id or "",
            "memory_type": candidate.memory_type,
            "content": candidate.content,
            "normalized_content": candidate.normalized_content,
            "memory_key": candidate.effective_memory_key,
            "memory_key_version": candidate.memory_key_version,
            "subject": candidate.subject,
            "predicate": candidate.predicate,
            "object_value": candidate.object_value,
            "task_status": candidate.task_status,
            "polarity": candidate.polarity,
            "scope_json": dict(candidate.scope),
            "entities_json": list(candidate.entities),
            "valid_from": _dt(candidate.valid_from),
            "valid_until": _dt(candidate.valid_until),
            "confidence": float(candidate.confidence),
            "importance": float(candidate.importance),
            "evidence_span": candidate.evidence_span,
            "clause_index": candidate.clause_index,
            "should_store": bool(candidate.should_store),
            "extractor_type": candidate.extractor_type,
            "extractor_version": candidate.extractor_version,
            "model": candidate.model,
            "prompt_hash": candidate.prompt_hash,
            "status": status,
            "last_error": error,
            "decision_id": decision_id,
            "updated_at": datetime.now().astimezone(),
        }
        session.execute(insert(MemoryCandidateRow).values(**values).on_conflict_do_nothing(index_elements=[MemoryCandidateRow.candidate_id]))
        row = session.get(MemoryCandidateRow, candidate.candidate_id)
        if row is None:
            raise RuntimeError(f"failed to persist memory candidate: {candidate.candidate_id}")
        return _candidate_record(row)


def get_memory_candidate(candidate_id: str, db_path: Any = None) -> MemoryCandidate | None:
    """函数功能：`get_memory_candidate` 负责获取 memory candidate，服务于本文件职责：Memory 数据访问。
    传参：
        candidate_id: candidate id 参数，由调用方传入，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryCandidate | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    with session_scope() as session:
        row = session.get(MemoryCandidateRow, candidate_id)
        return _candidate_record(row) if row is not None else None


def get_memory_candidate_status(candidate_id: str, db_path: Any = None) -> str | None:
    """函数功能：`get_memory_candidate_status` 负责获取 memory candidate status，服务于本文件职责：Memory 数据访问。
    传参：
        candidate_id: candidate id 参数，由调用方传入，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    with session_scope() as session:
        row = session.get(MemoryCandidateRow, candidate_id)
        return str(row.status) if row is not None else None


def mark_memory_candidate(
    candidate_id: str,
    status: str,
    *,
    error: str | None = None,
    decision_id: str | None = None,
    db_path: Any = None,
) -> bool:
    """函数功能：`mark_memory_candidate` 负责标记 memory candidate，服务于本文件职责：Memory 数据访问。
    传参：
        candidate_id: candidate id 参数，由调用方传入，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
        decision_id: decision id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    del db_path
    with session_scope() as session:
        row = session.get(MemoryCandidateRow, candidate_id)
        if row is None:
            return False
        row.status = status
        row.attempt_count += int(status == "processing")
        row.last_error = error
        if decision_id:
            row.decision_id = decision_id
        if status in {"applied", "pending_review", "discarded"}:
            row.applied_at = datetime.now().astimezone()
        return True


def list_retryable_memory_candidates(space_id: str, *, limit: int = 100, db_path: Any = None) -> list[MemoryCandidate]:
    """函数功能：`list_retryable_memory_candidates` 负责列出 retryable memory candidates，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `100`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryCandidate]`，表示按条件筛选、构造或查询得到的列表。
    """
    del db_path
    with session_scope() as session:
        rows = session.execute(
            select(MemoryCandidateRow)
            .where(MemoryCandidateRow.space_id == space_id, MemoryCandidateRow.status.in_(("extracted", "validated", "failed", "processing")))
            .order_by(MemoryCandidateRow.updated_at)
            .limit(max(1, min(int(limit), 500)))
        ).scalars()
        return [_candidate_record(row) for row in rows]


def update_memory(
    memory_id: str,
    *,
    content: str | None = None,
    status: str | None = None,
    task_status: str | None = None,
    valid_until: str | None = None,
    confidence: float | None = None,
    importance: float | None = None,
    last_confirmed_at: str | None = None,
    reason: str | None = None,
    source_note_id: str | None = None,
    db_path: Any = None,
) -> MemoryRecord | None:
    """函数功能：`update_memory` 负责更新 memory，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str | None`，默认值为 `None`。
        status: status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        task_status: task status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        valid_until: valid until 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        confidence: confidence 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        importance: importance 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        last_confirmed_at: last confirmed at 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        reason: reason 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    with session_scope() as session:
        row = session.execute(select(Memory).where(Memory.id == memory_id).with_for_update()).scalar_one_or_none()
        if row is None:
            return None
        _versioned_update(
            session, row, content=content, status=status, task_status=task_status, valid_until=valid_until,
            confidence=confidence, importance=importance, last_confirmed_at=last_confirmed_at,
            reason=reason, source_note_id=source_note_id,
        )
        return _record(session, row)


def apply_memory_decision(
    space_id: str,
    note_id: str,
    candidate: MemoryCandidate,
    decision: MemoryDecision,
    *,
    merged_content: str | None = None,
    db_path: Any = None,
) -> dict[str, Any]:
    """函数功能：`apply_memory_decision` 负责处理 apply memory decision，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        decision: decision 参数，由调用方传入，类型为 `MemoryDecision`。
        merged_content: merged content 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    del db_path
    try:
        with session_scope() as session:
            action = decision.recommended_action
            now = utc_now_iso()
            target_id = decision.target_memory_ids[0] if decision.target_memory_ids else None
            target = None
            if target_id:
                target = session.execute(select(Memory).where(Memory.id == target_id).with_for_update()).scalar_one_or_none()
                if target is None:
                    raise ValueError(f"decision target memory not found: {target_id}")
            result_ids: list[str] = []
            result: dict[str, Any] = {
                "action": action, "relation": decision.relation, "decision_id": decision.decision_id,
                "candidate_id": candidate.candidate_id, "confidence": decision.confidence,
            }
            if action == "discard":
                pass
            elif action in {"insert", "pending_review"}:
                row = _insert_memory(session, space_id, candidate, source_note_id=note_id, status="pending_review" if action == "pending_review" else "active", now=now)
                result_ids.append(row.id)
                result["memory_id"] = row.id
                if target_id:
                    result["target_memory_id"] = target_id
            elif action == "add_source" and target is not None:
                added = _add_source(session, target.id, note_id, "supported_by", now=now)
                if added:
                    old = float(target.confidence)
                    target.confidence = min(0.99, max(old, old + (candidate.confidence - old) * 0.25 + 0.02))
                    target.last_confirmed_at = _dt(now)
                    target.updated_at = _dt(now)
                result_ids.append(target.id)
                result.update({"memory_id": target.id, "source_added": added})
                if added and candidate.memory_type == "task" and candidate.task_status in {"done", "cancelled"}:
                    archived = _archive_terminal_task_duplicates(
                        session, target, candidate, decision_id=decision.decision_id, source_note_id=note_id, now=now,
                    )
                    if archived:
                        result["archived_duplicate_ids"] = archived
            elif action in {"merge", "update_task"} and target is not None:
                relation = "updated_by" if action == "update_task" else "supported_by"
                added = _add_source(session, target.id, note_id, relation, now=now)
                if added:
                    _versioned_update(
                        session,
                        target,
                        content=candidate.content if action == "update_task" else (merged_content or candidate.content),
                        task_status=candidate.task_status if action == "update_task" else None,
                        object_value=candidate.object_value if action == "update_task" else None,
                        scope=dict(candidate.scope) if action == "update_task" else None,
                        memory_key=candidate.effective_memory_key if action == "update_task" else None,
                        memory_key_version=candidate.memory_key_version if action == "update_task" else None,
                        confidence=min(0.99, max(float(target.confidence), candidate.confidence)),
                        importance=max(float(target.importance), candidate.importance) if action == "merge" else None,
                        last_confirmed_at=now,
                        reason=decision.reason,
                        source_note_id=note_id,
                    )
                result_ids.append(target.id)
                result.update({"memory_id": target.id, "source_added": added})
                if action == "update_task":
                    result["task_status"] = candidate.task_status
                    archived = _archive_terminal_task_duplicates(
                        session, target, candidate, decision_id=decision.decision_id, source_note_id=note_id, now=now,
                    )
                    if archived:
                        result["archived_duplicate_ids"] = archived
            elif action in {"supersede", "conflict"} and target is not None:
                _add_source(session, target.id, note_id, "contradicted_by", now=now)
                _versioned_update(
                    session, target, status="superseded" if action == "supersede" else "conflicted",
                    valid_until=now if action == "supersede" else None,
                    reason=decision.reason, source_note_id=note_id,
                )
                row = _insert_memory(
                    session, space_id, candidate, source_note_id=note_id,
                    status="active" if action == "supersede" else "conflicted", now=now,
                )
                pairs = [(row.id, target.id), (target.id, row.id)]
                relations = ("supersedes", "superseded_by") if action == "supersede" else ("conflicts_with", "conflicts_with")
                for pair, relation in zip(pairs, relations):
                    _add_relation(session, space_id, pair[0], pair[1], relation, decision.decision_id, now)
                result_ids.extend([row.id, target.id])
                result.update({"memory_id": row.id, "target_memory_id": target.id})
            else:
                raise ValueError(f"decision action cannot be applied: {action}")
            _save_decision(
                session, space_id, note_id, decision,
                status="pending_review" if action == "pending_review" else "applied",
                result_ids=result_ids,
            )
            return result
    except Exception as exc:
        try:
            with session_scope() as session:
                _save_decision(session, space_id, note_id, decision, status="failed", error=type(exc).__name__)
        except Exception:
            pass
        raise


def mark_accessed(memory_ids: list[str], db_path: Any = None) -> None:
    """函数功能：`mark_accessed` 负责标记 accessed，服务于本文件职责：Memory 数据访问。
    传参：
        memory_ids: memory ids 参数，由调用方传入，类型为 `list[str]`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    del db_path
    if not memory_ids:
        return
    now = utc_now_iso()
    if COORDINATION_BACKEND == "redis" and MEMORY_ACCESS_BUFFER_ENABLED:
        try:
            from infrastructure.redis_cache import MemoryAccessBuffer
            with session_scope() as session:
                rows = session.execute(select(Memory.id, Memory.tenant_id).where(Memory.id.in_(set(memory_ids)))).all()
            by_tenant: dict[str, list[str]] = {}
            for memory_id, tenant_id in rows:
                by_tenant.setdefault(str(tenant_id or DEFAULT_TENANT_ID), []).append(str(memory_id))
            buffer = MemoryAccessBuffer()
            for tenant_id, tenant_memory_ids in by_tenant.items():
                buffer.increment(tenant_memory_ids, seen_at=now, tenant_id=tenant_id)
        except Exception as exc:
            LOGGER.debug("memory access counter buffering failed: %s", type(exc).__name__)
        return
    with session_scope() as session:
        session.execute(
            update(Memory)
            .where(Memory.id.in_(set(memory_ids)))
            .values(last_accessed_at=_dt(now), access_count=Memory.access_count + 1)
        )


def flush_access_counts(
    *,
    limit: int = MEMORY_ACCESS_FLUSH_BATCH_SIZE,
    tenant_id: str = DEFAULT_TENANT_ID,
    db_path: Any = None,
) -> int:
    """函数功能：`flush_access_counts` 负责处理 flush access counts，服务于本文件职责：Memory 数据访问。
    传参：
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `MEMORY_ACCESS_FLUSH_BATCH_SIZE`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`，默认值为 `DEFAULT_TENANT_ID`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    del db_path
    if COORDINATION_BACKEND != "redis" or not MEMORY_ACCESS_BUFFER_ENABLED:
        return 0
    from sqlalchemy import Integer, String, column, values
    from infrastructure.redis_cache import MemoryAccessBuffer

    buffer = MemoryAccessBuffer()
    entries = buffer.drain(limit=max(1, int(limit)), tenant_id=tenant_id)
    if not entries:
        return 0
    updates = values(
        column("memory_id", String),
        column("increment", Integer),
        name="memory_access_updates",
    ).data([(memory_id, count) for memory_id, (count, _last_seen) in entries.items()])
    try:
        with session_scope() as session:
            session.execute(
                update(Memory)
                .where(Memory.id == updates.c.memory_id, Memory.tenant_id == tenant_id)
                .values(
                    last_accessed_at=_dt(utc_now_iso()),
                    access_count=Memory.access_count + updates.c.increment,
                )
            )
    except Exception:
        buffer.restore(entries, tenant_id=tenant_id)
        raise
    return len(entries)


def soft_delete_memory(memory_id: str, *, reason: str = "user_forget", db_path: Any = None) -> MemoryRecord | None:
    """函数功能：`soft_delete_memory` 负责软删除 delete memory，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        reason: reason 参数，由调用方传入，类型为 `str`，默认值为 `'user_forget'`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    return update_memory(memory_id, status="deleted", reason=reason, db_path=db_path)


def correct_memory(
    memory_id: str,
    content: str,
    *,
    task_status: str | None = None,
    reason: str = "user_correct",
    db_path: Any = None,
) -> MemoryRecord | None:
    """函数功能：`correct_memory` 负责处理 correct memory，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
        task_status: task status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        reason: reason 参数，由调用方传入，类型为 `str`，默认值为 `'user_correct'`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    from memory.task_state import infer_task_status, validate_task_status
    from memory.policies.task import can_transition

    with session_scope() as session:
        row = session.execute(select(Memory).where(Memory.id == memory_id).with_for_update()).scalar_one_or_none()
        if row is None:
            return None
        resolved_status = validate_task_status(task_status)
        if row.memory_type == "task":
            resolved_status = resolved_status or infer_task_status(content) or row.task_status
            if resolved_status != row.task_status and not can_transition(row.task_status, resolved_status):
                raise ValueError(f"invalid task status transition: {row.task_status} -> {resolved_status}")
            if row.task_status in {"done", "cancelled"} and resolved_status in {"todo", "in_progress"}:
                reopen_markers = ("重新", "再次", "重做", "返工", "恢复", "再开始", "又开始")
                if not any(marker in content for marker in reopen_markers):
                    raise ValueError("reopening a terminal task requires explicit wording")
        _versioned_update(
            session,
            row,
            content=content,
            status="active",
            task_status=resolved_status if row.memory_type == "task" else None,
            reason=reason,
            source_note_id=None,
        )
        return _record(session, row)


def purge_memory(memory_id: str, db_path: Any = None) -> bool:
    """函数功能：`purge_memory` 负责彻底清除 memory，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    del db_path
    with session_scope() as session:
        row = session.execute(select(Memory).where(Memory.id == memory_id).with_for_update()).scalar_one_or_none()
        if row is None:
            return False
        session.execute(delete(MemoryRelationRow).where(or_(MemoryRelationRow.source_memory_id == memory_id, MemoryRelationRow.target_memory_id == memory_id)))
        session.delete(row)
        return True


def approve_pending_memory(memory_id: str, db_path: Any = None) -> MemoryRecord | None:
    """函数功能：`approve_pending_memory` 负责批准 pending memory，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    with session_scope() as session:
        pending = session.execute(
            select(Memory).where(Memory.id == memory_id, Memory.status == "pending_review").with_for_update()
        ).scalar_one_or_none()
        if pending is None:
            return None
        decision = session.execute(
            select(MemoryDecisionRow)
            .where(
                MemoryDecisionRow.status == "pending_review",
                MemoryDecisionRow.result_memory_ids_json.contains([memory_id]),
            )
            .order_by(MemoryDecisionRow.created_at.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if decision is None:
            return None
        target_id = decision.target_memory_ids_json[0] if decision.target_memory_ids_json else None
        target = session.execute(select(Memory).where(Memory.id == target_id).with_for_update()).scalar_one_or_none() if target_id else None
        source_ids = list(session.execute(select(MemorySourceRow.note_id).where(MemorySourceRow.memory_id == memory_id)).scalars())
        source_note_id = source_ids[0] if source_ids else decision.note_id
        now = utc_now_iso()
        relation = decision.relation
        result = pending
        result_ids = [pending.id]
        if relation == "new":
            _versioned_update(session, pending, status="active", last_confirmed_at=now, reason="user_approved_pending_memory", source_note_id=source_note_id)
        elif relation in {"merge", "update_task"} and target is not None:
            from memory.policies import merge_content
            for note_id in source_ids:
                _add_source(session, target.id, note_id, "updated_by" if relation == "update_task" else "supported_by", now=now)
            _versioned_update(
                session, target,
                content=pending.content if relation == "update_task" else merge_content(pending.memory_type, target.content, pending.content),
                task_status=pending.task_status if relation == "update_task" else None,
                confidence=min(0.99, max(float(target.confidence), float(pending.confidence))),
                importance=max(float(target.importance), float(pending.importance)) if relation == "merge" else None,
                last_confirmed_at=now, reason=f"user_approved_{relation}", source_note_id=source_note_id,
            )
            _versioned_update(session, pending, status="archived", reason=f"{relation}_applied_after_review", source_note_id=source_note_id)
            if relation == "merge":
                _add_relation(session, pending.space_id, target.id, pending.id, "derived_from", decision.id, now)
            result = target
            result_ids = [target.id, pending.id]
        elif relation in {"supersede", "conflict"} and target is not None:
            _add_source(session, target.id, source_note_id, "contradicted_by", now=now)
            _versioned_update(
                session, target, status="superseded" if relation == "supersede" else "conflicted",
                valid_until=now if relation == "supersede" else None,
                reason=f"user_approved_{relation}", source_note_id=source_note_id,
            )
            _versioned_update(
                session, pending, status="active" if relation == "supersede" else "conflicted",
                last_confirmed_at=now, reason="user_approved_pending_memory", source_note_id=source_note_id,
            )
            rels = ("supersedes", "superseded_by") if relation == "supersede" else ("conflicts_with", "conflicts_with")
            _add_relation(session, pending.space_id, pending.id, target.id, rels[0], decision.id, now)
            _add_relation(session, pending.space_id, target.id, pending.id, rels[1], decision.id, now)
            result_ids = [pending.id, target.id]
        else:
            raise ValueError(f"unsupported pending review relation: {relation}")
        decision.status = "applied"
        decision.recommended_action = {"new": "insert", "merge": "merge", "update_task": "update_task", "supersede": "supersede", "conflict": "conflict"}[relation]
        decision.result_memory_ids_json = result_ids
        decision.reason += "; user_approved"
        decision.applied_at = _dt(now)
        session.flush()
        return _record(session, result)


def reject_pending_memory(memory_id: str, *, reason: str = "user_rejected_pending_memory", db_path: Any = None) -> MemoryRecord | None:
    """函数功能：`reject_pending_memory` 负责拒绝 pending memory，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        reason: reason 参数，由调用方传入，类型为 `str`，默认值为 `'user_rejected_pending_memory'`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    with session_scope() as session:
        row = session.execute(select(Memory).where(Memory.id == memory_id, Memory.status == "pending_review").with_for_update()).scalar_one_or_none()
        if row is None:
            return None
        decision = session.execute(
            select(MemoryDecisionRow)
            .where(MemoryDecisionRow.status == "pending_review", MemoryDecisionRow.result_memory_ids_json.contains([memory_id]))
            .order_by(MemoryDecisionRow.created_at.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        _versioned_update(session, row, status="archived", reason=reason, source_note_id=None)
        if decision is not None:
            decision.status = "rejected"
            decision.error = reason
            candidate = session.get(MemoryCandidateRow, decision.candidate_id)
            if candidate is not None:
                candidate.status = "discarded"
                candidate.last_error = reason
        session.flush()
        return _record(session, row)


def edit_pending_memory(memory_id: str, content: str, db_path: Any = None) -> MemoryRecord | None:
    """函数功能：`edit_pending_memory` 负责处理 edit pending memory，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    if not content.strip():
        return None
    from memory.task_state import infer_task_status
    from memory.policies.task import can_transition
    with session_scope() as session:
        row = session.execute(select(Memory).where(Memory.id == memory_id, Memory.status == "pending_review").with_for_update()).scalar_one_or_none()
        if row is None:
            return None
        next_status = infer_task_status(content) if row.memory_type == "task" else None
        _versioned_update(
            session,
            row,
            content=content,
            status="pending_review",
            task_status=next_status if next_status is not None else row.task_status if row.memory_type == "task" else None,
            reason="user_edited_pending_memory",
            source_note_id=None,
        )
        candidate = session.execute(select(MemoryCandidateRow).where(MemoryCandidateRow.candidate_id.in_(
            select(MemoryDecisionRow.candidate_id).where(
                MemoryDecisionRow.status == "pending_review",
                MemoryDecisionRow.result_memory_ids_json.contains([memory_id]),
            )
        ))).scalar_one_or_none()
        if candidate is not None and row.memory_type == "task" and next_status is not None:
            candidate.content = content
            candidate.task_status = next_status
            candidate.updated_at = datetime.now().astimezone()
        decision = session.execute(
            select(MemoryDecisionRow)
            .where(MemoryDecisionRow.status == "pending_review", MemoryDecisionRow.result_memory_ids_json.contains([memory_id]))
            .order_by(MemoryDecisionRow.created_at.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if decision is not None and row.memory_type == "task" and next_status is not None and decision.target_memory_ids_json:
            target = session.execute(select(Memory).where(Memory.id == decision.target_memory_ids_json[0]).with_for_update()).scalar_one_or_none()
            if target is not None and (next_status == target.task_status or can_transition(target.task_status, next_status)):
                decision.relation = "update_task"
                decision.recommended_action = "update_task"
                decision.reason = "user_edited_pending_memory_revalidated"
                decision.target_snapshot_version = target.current_version
    return approve_pending_memory(memory_id)


def resolve_memory_conflict(
    memory_id: str,
    *,
    resolution: str,
    content: str | None = None,
    db_path: Any = None,
) -> MemoryRecord | None:
    """函数功能：`resolve_memory_conflict` 负责解析 memory conflict，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        resolution: resolution 参数，由调用方传入，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    if resolution not in {"keep", "merge", "archive"}:
        raise ValueError("resolution must be keep, merge, or archive")
    with session_scope() as session:
        row = session.execute(select(Memory).where(Memory.id == memory_id).with_for_update()).scalar_one_or_none()
        if row is None:
            return None
        if resolution == "merge" and not content:
            raise ValueError("merge resolution requires content")
        _versioned_update(
            session,
            row,
            content=content if resolution == "merge" else None,
            status="active" if resolution in {"keep", "merge"} else "archived",
            reason=f"user_resolved_conflict_{resolution}",
            source_note_id=None,
        )
        session.flush()
        return _record(session, row)


def list_memory_decisions(space_id: str, *, note_id: str | None = None, status: str | None = None, limit: int = 50, db_path: Any = None) -> list[dict[str, Any]]:
    """函数功能：`list_memory_decisions` 负责列出 memory decisions，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str | None`，默认值为 `None`。
        status: status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `50`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    del db_path
    statement = select(MemoryDecisionRow).where(MemoryDecisionRow.space_id == space_id)
    if note_id:
        statement = statement.where(MemoryDecisionRow.note_id == note_id)
    if status:
        statement = statement.where(MemoryDecisionRow.status == status)
    statement = statement.order_by(MemoryDecisionRow.created_at.desc(), MemoryDecisionRow.id.desc()).limit(max(1, min(int(limit), 200)))
    with session_scope() as session:
        return [
            {
                "id": row.id, "space_id": row.space_id, "note_id": row.note_id, "candidate_id": row.candidate_id,
                "relation": row.relation, "target_memory_ids": list(row.target_memory_ids_json or []),
                "confidence": float(row.confidence), "reason": row.reason, "evidence": list(row.evidence_json or []),
                "recommended_action": row.recommended_action, "status": row.status,
                "result_memory_ids": list(row.result_memory_ids_json or []), "error": row.error,
                "created_at": _iso(row.created_at), "applied_at": _iso(row.applied_at),
            }
            for row in session.execute(statement).scalars()
        ]


def list_memory_relations(memory_id: str, *, db_path: Any = None) -> list[MemoryRelation]:
    """函数功能：`list_memory_relations` 负责列出 memory relations，服务于本文件职责：Memory 数据访问。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRelation]`，表示按条件筛选、构造或查询得到的列表。
    """
    del db_path
    with session_scope() as session:
        rows = session.execute(
            select(MemoryRelationRow)
            .where(or_(MemoryRelationRow.source_memory_id == memory_id, MemoryRelationRow.target_memory_id == memory_id))
            .order_by(MemoryRelationRow.created_at)
        ).scalars()
        return [
            MemoryRelation(row.id, row.space_id, row.source_memory_id, row.target_memory_id, row.relation, row.decision_id, _iso(row.created_at) or "")
            for row in rows
        ]


def add_memory_relation(space_id: str, source_memory_id: str, target_memory_id: str, relation: str, *, decision_id: str | None = None, db_path: Any = None) -> None:
    """函数功能：`add_memory_relation` 负责处理 add memory relation，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        source_memory_id: source memory id 参数，由调用方传入，类型为 `str`。
        target_memory_id: target memory id 参数，由调用方传入，类型为 `str`。
        relation: relation 参数，由调用方传入，类型为 `str`。
        decision_id: decision id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    del db_path
    with session_scope() as session:
        _add_relation(session, space_id, source_memory_id, target_memory_id, relation, decision_id, utc_now_iso())


def save_memory_trace(trace: dict[str, Any], db_path: Any = None) -> None:
    """函数功能：`save_memory_trace` 负责保存 memory trace，服务于本文件职责：Memory 数据访问。
    传参：
        trace: trace 参数，由调用方传入，类型为 `dict[str, Any]`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    del db_path
    space_id = str(trace.get("space_id") or "unknown")
    with session_scope() as session:
        values = {
            "space_id": space_id,
            "note_id": trace.get("note_id"),
            "trace_type": str(trace.get("trace_type") or "unknown"),
            "status": str(trace.get("status") or "unknown"),
            "payload_json": trace,
            "started_at": _dt(trace.get("started_at") or utc_now_iso()),
            "finished_at": _dt(trace.get("finished_at")),
        }
        session.execute(
            insert(MemoryTrace)
            .values(trace_id=str(trace["trace_id"]), **values)
            .on_conflict_do_update(index_elements=[MemoryTrace.trace_id], set_=values)
        )


def list_memory_traces(*, limit: int = 1000) -> list[dict[str, Any]]:
    """函数功能：`list_memory_traces` 负责列出 memory traces，服务于本文件职责：Memory 数据访问。
    传参：
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `1000`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    with session_scope() as session:
        rows = list(session.execute(
            select(MemoryTrace.payload_json)
            .order_by(func.coalesce(MemoryTrace.finished_at, MemoryTrace.started_at).desc())
            .limit(max(1, min(int(limit), 5000)))
        ).scalars())
    rows.reverse()
    return [dict(row or {}) for row in rows]


def note_has_memory(note_id: str, db_path: Any = None) -> bool:
    """函数功能：`note_has_memory` 负责判断是否包含 memory，服务于本文件职责：Memory 数据访问。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    del db_path
    with session_scope() as session:
        return session.execute(select(MemorySourceRow.memory_id).where(MemorySourceRow.note_id == note_id).limit(1)).scalar_one_or_none() is not None


def get_extraction_state(note_id: str, db_path: Any = None) -> MemoryExtractionState | None:
    """函数功能：`get_extraction_state` 负责获取 extraction state，服务于本文件职责：Memory 数据访问。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    with session_scope() as session:
        row = session.get(MemoryExtractionStateRow, note_id)
        return _state(row) if row is not None else None


def _mark_extraction_state(
    note_id: str,
    space_id: str,
    status: str,
    *,
    candidate_count: int = 0,
    processed_count: int = 0,
    error: str | None = None,
    increment_attempt: bool = False,
    db_path: Any = None,
) -> MemoryExtractionState:
    """函数功能：`_mark_extraction_state` 负责标记 extraction state，服务于本文件职责：Memory 数据访问。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`。
        candidate_count: candidate count 参数，由调用方传入，类型为 `int`，默认值为 `0`。
        processed_count: processed count 参数，由调用方传入，类型为 `int`，默认值为 `0`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
        increment_attempt: increment attempt 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    del db_path
    if status not in MEMORY_EXTRACTION_STATUSES:
        raise ValueError(f"invalid memory extraction status: {status}")
    now = utc_now_iso()
    with session_scope() as session:
        statement = insert(MemoryExtractionStateRow).values(
            note_id=note_id,
            space_id=space_id,
            status=status,
            candidate_count=max(0, int(candidate_count)),
            processed_count=max(0, int(processed_count)),
            attempt_count=1 if increment_attempt else 0,
            last_error=error,
            started_at=_dt(now) if status == "processing" else None,
            completed_at=_dt(now) if status in {"completed", "empty", "partial", "failed"} else None,
            updated_at=_dt(now),
        )
        values = {
            "space_id": space_id,
            "status": status,
            "candidate_count": max(0, int(candidate_count)),
            "processed_count": max(0, int(processed_count)),
            "attempt_count": MemoryExtractionStateRow.attempt_count + (1 if increment_attempt else 0),
            "last_error": error,
            "started_at": func.coalesce(statement.excluded.started_at, MemoryExtractionStateRow.started_at),
            "completed_at": _dt(now) if status in {"completed", "empty", "partial", "failed"} else None,
            "updated_at": _dt(now),
        }
        row = session.execute(
            statement
            .on_conflict_do_update(index_elements=[MemoryExtractionStateRow.note_id], set_=values)
            .returning(MemoryExtractionStateRow)
        ).scalar_one()
        return _state(row)


def mark_extraction_processing(note_id: str, space_id: str, db_path: Any = None) -> MemoryExtractionState:
    """函数功能：`mark_extraction_processing` 负责标记 extraction processing，服务于本文件职责：Memory 数据访问。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return _mark_extraction_state(note_id, space_id, "processing", increment_attempt=True, db_path=db_path)


def mark_extraction_completed(note_id: str, space_id: str, *, candidate_count: int, processed_count: int, db_path: Any = None) -> MemoryExtractionState:
    """函数功能：`mark_extraction_completed` 负责标记 extraction completed，服务于本文件职责：Memory 数据访问。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate_count: candidate count 参数，由调用方传入，类型为 `int`。
        processed_count: processed count 参数，由调用方传入，类型为 `int`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return _mark_extraction_state(note_id, space_id, "completed", candidate_count=candidate_count, processed_count=processed_count, db_path=db_path)


def mark_extraction_empty(note_id: str, space_id: str, db_path: Any = None) -> MemoryExtractionState:
    """函数功能：`mark_extraction_empty` 负责标记 extraction empty，服务于本文件职责：Memory 数据访问。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return _mark_extraction_state(note_id, space_id, "empty", db_path=db_path)


def mark_extraction_empty_attempt(note_id: str, space_id: str, db_path: Any = None) -> MemoryExtractionState:
    """函数功能：`mark_extraction_empty_attempt` 负责标记 extraction empty attempt，服务于本文件职责：Memory 数据访问。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return _mark_extraction_state(note_id, space_id, "empty", increment_attempt=True, db_path=db_path)


def mark_extraction_partial(note_id: str, space_id: str, *, candidate_count: int, processed_count: int, error: str, db_path: Any = None) -> MemoryExtractionState:
    """函数功能：`mark_extraction_partial` 负责标记 extraction partial，服务于本文件职责：Memory 数据访问。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate_count: candidate count 参数，由调用方传入，类型为 `int`。
        processed_count: processed count 参数，由调用方传入，类型为 `int`。
        error: 当前捕获的异常对象，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return _mark_extraction_state(note_id, space_id, "partial", candidate_count=candidate_count, processed_count=processed_count, error=error, db_path=db_path)


def mark_extraction_failed(note_id: str, space_id: str, *, error: str, db_path: Any = None) -> MemoryExtractionState:
    """函数功能：`mark_extraction_failed` 负责标记 extraction failed，服务于本文件职责：Memory 数据访问。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return _mark_extraction_state(note_id, space_id, "failed", error=error, db_path=db_path)


def list_retryable_extraction_states(space_id: str, *, limit: int = 100, db_path: Any = None) -> list[MemoryExtractionState]:
    """函数功能：`list_retryable_extraction_states` 负责列出 retryable extraction states，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `100`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryExtractionState]`，表示按条件筛选、构造或查询得到的列表。
    """
    del db_path
    with session_scope() as session:
        rows = session.execute(
            select(MemoryExtractionStateRow)
            .where(MemoryExtractionStateRow.space_id == space_id, MemoryExtractionStateRow.status.in_(["pending", "failed", "partial"]))
            .order_by(MemoryExtractionStateRow.updated_at)
            .limit(max(1, min(int(limit), 500)))
        ).scalars()
        return [_state(row) for row in rows]


def consolidation_period_key(cadence: str, day: date) -> str:
    """函数功能：`consolidation_period_key` 负责处理 consolidation period key，服务于本文件职责：Memory 数据访问。
    传参：
        cadence: cadence 参数，由调用方传入，类型为 `str`。
        day: day 参数，由调用方传入，类型为 `date`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    cadence = cadence.strip().lower()
    if cadence == "daily":
        return day.isoformat()
    if cadence == "weekly":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    if cadence == "monthly":
        return f"{day.year:04d}-{day.month:02d}"
    raise ValueError(f"unknown memory consolidation cadence: {cadence}")


def _stale(value: str | datetime | None) -> bool:
    """函数功能：`_stale` 负责处理 stale，服务于本文件职责：Memory 数据访问。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | datetime | None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if not value:
        return True
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return (datetime.now().astimezone() - parsed).total_seconds() > MEMORY_CONSOLIDATION_RUN_LEASE_SECONDS


def reserve_consolidation_run(space_id: str, cadence: str, period_key: str, db_path: Any = None) -> ConsolidationRun | None:
    """函数功能：`reserve_consolidation_run` 负责预约 consolidation run，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        cadence: cadence 参数，由调用方传入，类型为 `str`。
        period_key: period key 参数，由调用方传入，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `ConsolidationRun | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    cadence = cadence.strip().lower()
    with session_scope() as session:
        old = session.execute(
            select(MemoryConsolidationRun)
            .where(MemoryConsolidationRun.space_id == space_id, MemoryConsolidationRun.cadence == cadence, MemoryConsolidationRun.period_key == period_key)
            .with_for_update()
        ).scalar_one_or_none()
        if old is not None and (old.status == "completed" or (old.status == "running" and not _stale(old.started_at))):
            return None
        values = {"status": "running", "started_at": _dt(utc_now_iso()), "completed_at": None, "error": None, "result_json": None}
        if old is None:
            old = MemoryConsolidationRun(id=new_id("run"), space_id=space_id, cadence=cadence, period_key=period_key, **values)
            session.add(old)
        else:
            old.id = new_id("run")
            for key, value in values.items():
                setattr(old, key, value)
        session.flush()
        return _run(old)


def get_consolidation_run(run_id: str, db_path: Any = None) -> ConsolidationRun | None:
    """函数功能：`get_consolidation_run` 负责获取 consolidation run，服务于本文件职责：Memory 数据访问。
    传参：
        run_id: run id 参数，由调用方传入，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `ConsolidationRun | None`；未命中或无需处理时可返回 `None`。
    """
    del db_path
    with session_scope() as session:
        row = session.get(MemoryConsolidationRun, run_id)
        return _run(row) if row is not None else None


def mark_consolidation_completed(run_id: str, result: dict[str, Any], db_path: Any = None) -> None:
    """函数功能：`mark_consolidation_completed` 负责标记 consolidation completed，服务于本文件职责：Memory 数据访问。
    传参：
        run_id: run id 参数，由调用方传入，类型为 `str`。
        result: 上游步骤返回的结果对象，类型为 `dict[str, Any]`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    del db_path
    with session_scope() as session:
        row = session.execute(select(MemoryConsolidationRun).where(MemoryConsolidationRun.id == run_id).with_for_update()).scalar_one_or_none()
        if row is not None:
            row.status, row.completed_at, row.error, row.result_json = "completed", _dt(utc_now_iso()), None, result


def mark_consolidation_failed(run_id: str, error: str, db_path: Any = None) -> None:
    """函数功能：`mark_consolidation_failed` 负责标记 consolidation failed，服务于本文件职责：Memory 数据访问。
    传参：
        run_id: run id 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    del db_path
    with session_scope() as session:
        row = session.execute(select(MemoryConsolidationRun).where(MemoryConsolidationRun.id == run_id).with_for_update()).scalar_one_or_none()
        if row is not None:
            row.status, row.completed_at, row.error = "failed", _dt(utc_now_iso()), error


def hybrid_search_memory_hits(
    space_id: str,
    query: str,
    *,
    memory_type: str | None = None,
    include_inactive: bool = False,
    query_embedding: list[float] | None = None,
    limit: int = 40,
    db_path: Any = None,
) -> list[MemoryRetrievalHit]:
    """函数功能：`hybrid_search_memory_hits` 负责搜索 memory hits，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        include_inactive: include inactive 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        query_embedding: query embedding 参数，由调用方传入，类型为 `list[float] | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `40`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRetrievalHit]`，表示按条件筛选、构造或查询得到的列表。
    """
    del db_path
    top = max(1, min(int(limit), 100))
    retrieval_limit = max(30, min(120, top * 3))
    terms = _query_terms(query)
    embedding = query_embedding
    channels: list[tuple[str, list[Memory]]] = []
    _, embedding_dimension, _ = current_embedding_contract()
    with session_scope() as session:
        base = _base_memory_statement(space_id, memory_type=memory_type, include_inactive=include_inactive)
        # 状态层查询以精确命中优先：canonical key 或规范化原句必须排在近似语义匹配前。
        normalized_query = normalize_content(query)
        #memory 标准化内容是否和问题完全相同
        exact_filters = [Memory.normalized_content == normalized_query]
        if query.strip():
            #memory key是否完全相同
            #memory 内容是否完整包含查询
            exact_filters.extend(
                [
                    Memory.memory_key == query.strip(),
                    Memory.content.ilike(f"%{query.strip()[:160]}%"),
                ]
            )
        #查询中含有TASK-123、README-01 等编号时，在结构化字段中精确找编编号
        identifiers = re.findall(r"[\u4e00-\u9fffA-Za-z]+-\d+|[A-Za-z][A-Za-z0-9+#._-]*-\d+", query)
        for identifier in identifiers[:4]:
            escaped = re.escape(identifier[:120])
            identifier_pattern = rf"(^|[^[:alnum:]一-鿿]){escaped}([^[:alnum:]一-鿿]|$)"
            exact_filters.extend(
                [
                    Memory.content.op("~*")(identifier_pattern),
                    Memory.subject.op("~*")(identifier_pattern),
                    Memory.object_value.op("~*")(identifier_pattern),
                    Memory.memory_key.op("~*")(identifier_pattern),
                ]
            )
        exact_rows = list(
            session.execute(
                base.where(or_(*exact_filters))
                .order_by(Memory.updated_at.desc(), Memory.id.desc())
                .limit(min(retrieval_limit, 30))
            ).scalars()
        )
        if exact_rows:
            channels.append(("exact", exact_rows))
        type_hints: list[str] = []
        if any(marker in query for marker in ("喜欢", "偏好", "习惯", "讨厌", "避开", "过敏")):
            type_hints.append("preference")
        if any(marker in query for marker in ("任务", "待办", "要做", "进度", "完成", "取消")):
            type_hints.append("task")
        if any(marker in query for marker in ("住哪", "住在", "哪里", "项目", "学习", "研究")):
            type_hints.append("semantic")
        if type_hints and memory_type is None:
            channels.append((
                "structured",
                list(
                    session.execute(
                        base.where(Memory.memory_type.in_(type_hints))
                        .order_by(Memory.updated_at.desc(), Memory.id.desc())
                        .limit(retrieval_limit)
                    ).scalars()
                )
            ))

        structured_filters = [Memory.content.ilike(f"%{term[:120]}%") for term in terms[:12]]
        for term in terms[:12]:
            structured_filters.extend(
                [
                    Memory.subject.ilike(f"%{term[:120]}%"),
                    Memory.predicate.ilike(f"%{term[:120]}%"),
                    Memory.object_value.ilike(f"%{term[:120]}%"),
                ]
            )
        if structured_filters:
            channels.append((
                "structured",
                list(
                    session.execute(
                        base.where(or_(*structured_filters))
                        .order_by(Memory.updated_at.desc(), Memory.id.desc())
                        .limit(retrieval_limit)
                    ).scalars()
                )
            ))

        query_text = " ".join(terms) or query
        if query_text.strip():
            document = _text_document()
            tsquery = func.plainto_tsquery("simple", query_text[:500])
            channels.append((
                "fts",
                list(
                    session.execute(
                        base.where(document.op("@@")(tsquery))
                        .order_by(func.ts_rank_cd(document, tsquery).desc(), Memory.updated_at.desc())
                        .limit(retrieval_limit)
                    ).scalars()
                )
            ))
            if MEMORY_TRIGRAM_ENABLED and terms:
                trigram_filters = [Memory.content.op("%")(term[:120]) for term in terms[:8]]
                trigram_filters.extend(Memory.object_value.op("%")(term[:120]) for term in terms[:8])
                channels.append((
                    "trigram",
                    list(
                        session.execute(
                            base.where(or_(*trigram_filters))
                            .order_by(
                                func.greatest(
                                    *[func.similarity(Memory.content, term[:120]) for term in terms[:8]],
                                    *[func.similarity(func.coalesce(Memory.object_value, ""), term[:120]) for term in terms[:8]],
                                ).desc(),
                                Memory.updated_at.desc(),
                            )
                            .limit(retrieval_limit)
                        ).scalars()
                    ),
                ))

        if embedding and len(embedding) == embedding_dimension:
            model, dimension, version = current_embedding_contract()
            channels.append((
                "vector",
                list(
                    session.execute(
                        base.join(MemoryVector, MemoryVector.memory_id == Memory.id)
                        .where(
                            MemoryVector.status == "ready",
                            MemoryVector.embedding.is_not(None),
                            MemoryVector.model == model,
                            MemoryVector.dimension == dimension,
                            MemoryVector.embedding_version == version,
                        )
                        .order_by(MemoryVector.embedding.cosine_distance(embedding), Memory.updated_at.desc())
                        .limit(retrieval_limit)
                    ).scalars()
                )
            ))

        hits = _rrf_hits(channels, limit=top)
        ids = [hit.memory.id for hit in hits]
        if not ids:
            return []
        rows = list(session.execute(select(Memory).where(Memory.id.in_(ids))).scalars())
        row_map = {row.id: row for row in rows}
        records = _records(session, [row_map[memory_id] for memory_id in ids if memory_id in row_map], include_sources=True)
        record_map = {record.id: record for record in records}
        hydrated: list[MemoryRetrievalHit] = []
        for hit in hits:
            record = record_map.get(hit.memory.id)
            if record is None:
                continue
            hit.memory = record
            hydrated.append(hit)
        return hydrated


def hybrid_search_memories(
    space_id: str,
    query: str,
    *,
    memory_type: str | None = None,
    include_inactive: bool = False,
    query_embedding: list[float] | None = None,
    limit: int = 40,
    db_path: Any = None,
) -> list[MemoryRecord]:
    """函数功能：`hybrid_search_memories` 负责搜索 memories，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        include_inactive: include inactive 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        query_embedding: query embedding 参数，由调用方传入，类型为 `list[float] | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `40`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRecord]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [
        hit.memory
        for hit in hybrid_search_memory_hits(
            space_id,
            query,
            memory_type=memory_type,
            include_inactive=include_inactive,
            query_embedding=query_embedding,
            limit=limit,
            db_path=db_path,
        )
    ]


def search_memories(
    space_id: str,
    query: str,
    *,
    memory_type: str | None = None,
    include_inactive: bool = False,
    min_score: float = MEMORY_QUERY_MIN_SCORE,
    limit: int = 10,
    mark_access: bool = True,
    db_path: Any = None,
) -> list[tuple[MemoryRecord, float]]:
    """函数功能：`search_memories` 负责搜索 memories，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        include_inactive: include inactive 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        min_score: min score 参数，由调用方传入，类型为 `float`，默认值为 `MEMORY_QUERY_MIN_SCORE`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `10`。
        mark_access: mark access 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `list[tuple[MemoryRecord, float]]`，表示按条件筛选、构造或查询得到的列表。
    """
    from memory.retriever import score_memory

    if MEMORY_RETRIEVAL_MODE not in {"hybrid", "hybrid_v1", "hybrid_v2"}:
        candidates = list_memories(
            space_id,
            status=None if include_inactive else "active",
            memory_type=memory_type,
            limit=100,
            db_path=db_path,
        )
        scored = sorted(
            ((item, score_memory(query, item)) for item in candidates),
            key=lambda item: (item[1], item[0].updated_at, item[0].id),
            reverse=True,
        )
    else:
        hits = hybrid_search_memory_hits(
            space_id,
            query,
            memory_type=memory_type,
            include_inactive=include_inactive,
            query_embedding=_safe_embedding(space_id, query, memory_type=memory_type),
            limit=max(limit * 4, 30),
            db_path=db_path,
        )
        maximum_rrf = max((hit.final_score for hit in hits), default=0.0)
        ranked: list[tuple[MemoryRecord, float, MemoryRetrievalHit]] = []
        for hit in hits:
            policy = score_memory(query, hit.memory)
            rrf = hit.final_score / maximum_rrf if maximum_rrf > 0 else 0.0
            channel_count = sum(
                rank is not None
                for rank in (hit.exact_rank, hit.structured_rank, hit.fts_rank, hit.trigram_rank, hit.vector_rank)
            )
            fused_signal = min(1.0, rrf + 0.04 * max(0, channel_count - 1))
            final = 0.52 * policy + 0.48 * fused_signal
            if hit.exact_rank is not None:
                final = max(final, 0.98 - 0.005 * max(0, hit.exact_rank - 1))
                hit.reasons.append("exact_first_final")
            hit.policy_score += policy
            hit.final_score = round(min(1.0, final), 4)
            hit.reasons.append("deterministic_policy_rerank")
            ranked.append((hit.memory, hit.final_score, hit))
        ranked.sort(
            key=lambda item: (
                item[2].exact_rank is not None,
                item[1],
                item[2].rrf_score,
                item[0].updated_at,
                item[0].id,
            ),
            reverse=True,
        )
        scored = [(memory, score) for memory, score, _hit in ranked]

    if MEMORY_RETRIEVAL_MODE == "hybrid_v2" or MEMORY_UNIFIED_RERANK_ENABLED:
        limited = scored[: max(1, min(int(limit), 50))]
    else:
        limited = [(item, score) for item, score in scored if score >= min_score][: max(1, min(int(limit), 50))]
    if mark_access:
        mark_accessed([item.id for item, _ in limited])
    return limited


def stats(space_id: str, db_path: Any = None) -> dict[str, Any]:
    """函数功能：`stats` 负责处理 stats，服务于本文件职责：Memory 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    del db_path
    with session_scope() as session:
        memory_rows = session.execute(
            select(Memory.memory_type, Memory.status, func.count()).where(Memory.space_id == space_id).group_by(Memory.memory_type, Memory.status)
        )
        extraction_rows = list(session.execute(
            select(MemoryExtractionStateRow.status, func.count()).where(MemoryExtractionStateRow.space_id == space_id).group_by(MemoryExtractionStateRow.status)
        ))
        decision_rows = list(session.execute(
            select(MemoryDecisionRow.relation, MemoryDecisionRow.status, func.count()).where(MemoryDecisionRow.space_id == space_id).group_by(MemoryDecisionRow.relation, MemoryDecisionRow.status)
        ))
        runs = list(session.execute(
            select(MemoryConsolidationRun).where(MemoryConsolidationRun.space_id == space_id).order_by(MemoryConsolidationRun.started_at.desc()).limit(5)
        ).scalars())
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        total = 0
        for memory_type, status, count in memory_rows:
            count = int(count)
            total += count
            by_type[str(memory_type)] = by_type.get(str(memory_type), 0) + count
            by_status[str(status)] = by_status.get(str(status), 0) + count
        extraction = {str(status): int(count) for status, count in extraction_rows}
        decision_relation: dict[str, int] = {}
        decision_status: dict[str, int] = {}
        for relation, status, count in decision_rows:
            decision_relation[str(relation)] = decision_relation.get(str(relation), 0) + int(count)
            decision_status[str(status)] = decision_status.get(str(status), 0) + int(count)
        return {
            "total": total,
            "by_type": by_type,
            "by_status": by_status,
            "extraction_by_status": extraction,
            "retryable_extraction_count": sum(extraction.get(key, 0) for key in ("pending", "failed", "partial")),
            "decisions_by_relation": decision_relation,
            "decisions_by_status": decision_status,
            "consolidation_last_runs": [
                {"id": row.id, "cadence": row.cadence, "period_key": row.period_key, "status": row.status, "started_at": row.started_at, "completed_at": row.completed_at, "error": row.error}
                for row in runs
            ],
        }


def schema_tables(db_path: Any = None) -> set[str]:
    """函数功能：`schema_tables` 负责处理 schema tables，服务于本文件职责：Memory 数据访问。
    传参：
        db_path: db path 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `set[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    del db_path
    from sqlalchemy import inspect
    from infrastructure.database import get_engine
    return set(inspect(get_engine()).get_table_names())
