"""PostgreSQL implementation of the versioned Memory V2 repository."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from core.settings import MEMORY_CONSOLIDATION_RUN_LEASE_SECONDS, MEMORY_QUERY_MIN_SCORE
from infrastructure.database import session_scope
from infrastructure.schema import (
    Memory,
    MemoryConsolidationRun,
    MemoryDecision as MemoryDecisionRow,
    MemoryExtractionState as MemoryExtractionStateRow,
    MemoryRelation as MemoryRelationRow,
    MemorySource as MemorySourceRow,
    MemoryTrace,
    MemoryVersion as MemoryVersionRow,
    Space,
)
from memory.models import (
    MEMORY_EXTRACTION_STATUSES,
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
    new_id,
    normalize_content,
    utc_now_iso,
)
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space


def init_db(db_path: Any = None) -> None:
    del db_path


def _sources(session: Any, memory_id: str) -> list[MemorySource]:
    rows = session.execute(
        select(MemorySourceRow).where(MemorySourceRow.memory_id == memory_id).order_by(MemorySourceRow.created_at)
    ).scalars()
    return [MemorySource(row.memory_id, row.note_id, row.relation, row.created_at) for row in rows]


def _versions(session: Any, memory_id: str) -> list[MemoryVersion]:
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
            created_at=row.created_at,
            task_status=row.task_status,
            confidence=row.confidence,
            importance=row.importance,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
        )
        for row in rows
    ]


def _record(session: Any, row: Memory, *, include_versions: bool = True) -> MemoryRecord:
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
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_accessed_at=row.last_accessed_at,
        access_count=row.access_count,
        current_version=row.current_version,
        subject=row.subject,
        predicate=row.predicate,
        object_value=row.object_value,
        last_confirmed_at=row.last_confirmed_at,
        sources=_sources(session, row.id),
        versions=_versions(session, row.id) if include_versions else [],
    )


def _state(row: MemoryExtractionStateRow) -> MemoryExtractionState:
    return MemoryExtractionState(
        row.note_id, row.space_id, row.status, row.candidate_count, row.processed_count,
        row.attempt_count, row.last_error, row.started_at, row.completed_at, row.updated_at,
    )


def _run(row: MemoryConsolidationRun) -> ConsolidationRun:
    return ConsolidationRun(
        row.id,
        row.space_id,
        row.cadence,
        row.period_key,
        row.status,
        row.started_at,
        row.completed_at,
        row.error,
        json.dumps(row.result_json, ensure_ascii=False) if row.result_json is not None else None,
    )


def _add_source(session: Any, memory_id: str, note_id: str, relation: str, *, now: str | None = None) -> bool:
    if relation not in SOURCE_RELATIONS:
        raise ValueError(f"invalid source relation: {relation}")
    created = session.execute(
        insert(MemorySourceRow)
        .values(memory_id=memory_id, note_id=note_id, relation=relation, created_at=now or utc_now_iso())
        .on_conflict_do_nothing()
        .returning(MemorySourceRow.memory_id)
    ).scalar_one_or_none()
    return created is not None


def _add_version(session: Any, row: Memory, *, reason: str | None, source_note_id: str | None) -> None:
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
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            reason=reason,
            source_note_id=source_note_id,
            created_at=utc_now_iso(),
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
    if status not in MEMORY_STATUSES:
        raise ValueError(f"invalid memory status: {status}")
    space = session.get(Space, space_id)
    tenant_id = str(space.tenant_id) if space is not None else DEFAULT_TENANT_ID
    ensure_tenant_space(session, space_id, tenant_id=tenant_id)
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
        valid_from=candidate.valid_from or timestamp,
        valid_until=candidate.valid_until,
        last_confirmed_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
        current_version=1,
    )
    session.add(row)
    session.flush()
    _add_source(session, row.id, source_note_id, source_relation, now=timestamp)
    _add_version(session, row, reason=candidate.effective_reason or "memory_created", source_note_id=source_note_id)
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
    reason: str | None,
    source_note_id: str | None,
) -> None:
    if status is not None and status not in MEMORY_STATUSES:
        raise ValueError(f"invalid memory status: {status}")
    if content is not None:
        row.content = content
        row.normalized_content = normalize_content(content)
    if status is not None:
        row.status = status
    if task_status is not None:
        row.task_status = task_status
    if valid_until is not None:
        row.valid_until = valid_until
    if confidence is not None:
        row.confidence = float(confidence)
    if importance is not None:
        row.importance = float(importance)
    if last_confirmed_at is not None:
        row.last_confirmed_at = last_confirmed_at
    row.updated_at = utc_now_iso()
    row.current_version += 1
    session.flush()
    _add_version(session, row, reason=reason, source_note_id=source_note_id)


def _add_relation(session: Any, space_id: str, source_id: str, target_id: str, relation: str, decision_id: str | None, now: str) -> None:
    if relation not in MEMORY_RELATION_TYPES:
        raise ValueError(f"invalid memory relation: {relation}")
    session.execute(
        insert(MemoryRelationRow)
        .values(
            id=new_id("rel"), space_id=space_id, source_memory_id=source_id,
            target_memory_id=target_id, relation=relation, decision_id=decision_id, created_at=now,
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
            created_at=now,
            applied_at=now if status == "applied" else None,
        )
        .on_conflict_do_update(
            index_elements=[MemoryDecisionRow.id],
            set_={"status": status, "result_memory_ids_json": list(result_ids or []), "error": error},
        )
    )


def add_source(memory_id: str, note_id: str, relation: str, db_path: Any = None) -> bool:
    del db_path
    with session_scope() as session:
        row = session.execute(select(Memory).where(Memory.id == memory_id).with_for_update()).scalar_one_or_none()
        return False if row is None else _add_source(session, memory_id, note_id, relation)


def insert_memory(space_id: str, candidate: MemoryCandidate, *, source_note_id: str, source_relation: str = "created_from", status: str = "active", db_path: Any = None) -> MemoryRecord:
    del db_path
    with session_scope() as session:
        row = _insert_memory(session, space_id, candidate, source_note_id=source_note_id, source_relation=source_relation, status=status)
        session.flush()
        result = _record(session, row)
    return result


def get_memory(memory_id: str, db_path: Any = None) -> MemoryRecord | None:
    del db_path
    with session_scope() as session:
        row = session.get(Memory, memory_id)
        return _record(session, row) if row is not None else None


def list_memories(space_id: str, *, status: str | None = "active", memory_type: str | None = None, limit: int = 20, db_path: Any = None) -> list[MemoryRecord]:
    del db_path
    statement = select(Memory).where(Memory.space_id == space_id)
    if status:
        statement = statement.where(Memory.status == status)
    if memory_type:
        statement = statement.where(Memory.memory_type == memory_type)
    statement = statement.order_by(Memory.updated_at.desc(), Memory.id.desc()).limit(max(1, min(int(limit), 100)))
    with session_scope() as session:
        return [_record(session, row, include_versions=False) for row in session.execute(statement).scalars()]


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
    del db_path
    try:
        with session_scope() as session:
            ensure_tenant_space(session, space_id)
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
                    target.last_confirmed_at = now
                    target.updated_at = now
                result_ids.append(target.id)
                result.update({"memory_id": target.id, "source_added": added})
            elif action in {"merge", "update_task"} and target is not None:
                relation = "updated_by" if action == "update_task" else "supported_by"
                added = _add_source(session, target.id, note_id, relation, now=now)
                if added:
                    _versioned_update(
                        session,
                        target,
                        content=candidate.content if action == "update_task" else (merged_content or candidate.content),
                        task_status=candidate.task_status if action == "update_task" else None,
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
                ensure_tenant_space(session, space_id)
                _save_decision(session, space_id, note_id, decision, status="failed", error=type(exc).__name__)
        except Exception:
            pass
        raise


def mark_accessed(memory_ids: list[str], db_path: Any = None) -> None:
    del db_path
    if not memory_ids:
        return
    now = utc_now_iso()
    with session_scope() as session:
        rows = session.execute(select(Memory).where(Memory.id.in_(memory_ids)).with_for_update()).scalars()
        for row in rows:
            row.last_accessed_at = now
            row.access_count += 1


def soft_delete_memory(memory_id: str, *, reason: str = "user_forget", db_path: Any = None) -> MemoryRecord | None:
    return update_memory(memory_id, status="deleted", reason=reason, db_path=db_path)


def correct_memory(memory_id: str, content: str, *, reason: str = "user_correct", db_path: Any = None) -> MemoryRecord | None:
    return update_memory(memory_id, content=content, status="active", reason=reason, db_path=db_path)


def purge_memory(memory_id: str, db_path: Any = None) -> bool:
    del db_path
    with session_scope() as session:
        row = session.execute(select(Memory).where(Memory.id == memory_id).with_for_update()).scalar_one_or_none()
        if row is None:
            return False
        session.execute(delete(MemoryRelationRow).where(or_(MemoryRelationRow.source_memory_id == memory_id, MemoryRelationRow.target_memory_id == memory_id)))
        session.delete(row)
        return True


def approve_pending_memory(memory_id: str, db_path: Any = None) -> MemoryRecord | None:
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
        decision.applied_at = now
        session.flush()
        return _record(session, result)


def list_memory_decisions(space_id: str, *, note_id: str | None = None, status: str | None = None, limit: int = 50, db_path: Any = None) -> list[dict[str, Any]]:
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
                "created_at": row.created_at, "applied_at": row.applied_at,
            }
            for row in session.execute(statement).scalars()
        ]


def list_memory_relations(memory_id: str, *, db_path: Any = None) -> list[MemoryRelation]:
    del db_path
    with session_scope() as session:
        rows = session.execute(
            select(MemoryRelationRow)
            .where(or_(MemoryRelationRow.source_memory_id == memory_id, MemoryRelationRow.target_memory_id == memory_id))
            .order_by(MemoryRelationRow.created_at)
        ).scalars()
        return [MemoryRelation(row.id, row.space_id, row.source_memory_id, row.target_memory_id, row.relation, row.decision_id, row.created_at) for row in rows]


def add_memory_relation(space_id: str, source_memory_id: str, target_memory_id: str, relation: str, *, decision_id: str | None = None, db_path: Any = None) -> None:
    del db_path
    with session_scope() as session:
        _add_relation(session, space_id, source_memory_id, target_memory_id, relation, decision_id, utc_now_iso())


def save_memory_trace(trace: dict[str, Any], db_path: Any = None) -> None:
    del db_path
    space_id = str(trace.get("space_id") or "unknown")
    with session_scope() as session:
        ensure_tenant_space(session, space_id)
        values = {
            "space_id": space_id,
            "note_id": trace.get("note_id"),
            "trace_type": str(trace.get("trace_type") or "unknown"),
            "status": str(trace.get("status") or "unknown"),
            "payload_json": trace,
            "started_at": str(trace.get("started_at") or utc_now_iso()),
            "finished_at": trace.get("finished_at"),
        }
        session.execute(
            insert(MemoryTrace)
            .values(trace_id=str(trace["trace_id"]), **values)
            .on_conflict_do_update(index_elements=[MemoryTrace.trace_id], set_=values)
        )


def list_memory_traces(*, limit: int = 1000) -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = list(session.execute(
            select(MemoryTrace.payload_json)
            .order_by(func.coalesce(MemoryTrace.finished_at, MemoryTrace.started_at).desc())
            .limit(max(1, min(int(limit), 5000)))
        ).scalars())
    rows.reverse()
    return [dict(row or {}) for row in rows]


def note_has_memory(note_id: str, db_path: Any = None) -> bool:
    del db_path
    with session_scope() as session:
        return session.execute(select(MemorySourceRow.memory_id).where(MemorySourceRow.note_id == note_id).limit(1)).scalar_one_or_none() is not None


def get_extraction_state(note_id: str, db_path: Any = None) -> MemoryExtractionState | None:
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
    del db_path
    if status not in MEMORY_EXTRACTION_STATUSES:
        raise ValueError(f"invalid memory extraction status: {status}")
    now = utc_now_iso()
    with session_scope() as session:
        ensure_tenant_space(session, space_id)
        old = session.execute(
            select(MemoryExtractionStateRow).where(MemoryExtractionStateRow.note_id == note_id).with_for_update()
        ).scalar_one_or_none()
        attempt_count = (old.attempt_count if old else 0) + (1 if increment_attempt else 0)
        values = {
            "space_id": space_id,
            "status": status,
            "candidate_count": max(0, int(candidate_count)),
            "processed_count": max(0, int(processed_count)),
            "attempt_count": attempt_count,
            "last_error": error,
            "started_at": now if status == "processing" else (old.started_at if old else None),
            "completed_at": now if status in {"completed", "empty", "partial", "failed"} else None,
            "updated_at": now,
        }
        session.execute(
            insert(MemoryExtractionStateRow)
            .values(note_id=note_id, **values)
            .on_conflict_do_update(index_elements=[MemoryExtractionStateRow.note_id], set_=values)
        )
        session.flush()
        return _state(session.get(MemoryExtractionStateRow, note_id))


def mark_extraction_processing(note_id: str, space_id: str, db_path: Any = None) -> MemoryExtractionState:
    return _mark_extraction_state(note_id, space_id, "processing", increment_attempt=True, db_path=db_path)


def mark_extraction_completed(note_id: str, space_id: str, *, candidate_count: int, processed_count: int, db_path: Any = None) -> MemoryExtractionState:
    return _mark_extraction_state(note_id, space_id, "completed", candidate_count=candidate_count, processed_count=processed_count, db_path=db_path)


def mark_extraction_empty(note_id: str, space_id: str, db_path: Any = None) -> MemoryExtractionState:
    return _mark_extraction_state(note_id, space_id, "empty", db_path=db_path)


def mark_extraction_partial(note_id: str, space_id: str, *, candidate_count: int, processed_count: int, error: str, db_path: Any = None) -> MemoryExtractionState:
    return _mark_extraction_state(note_id, space_id, "partial", candidate_count=candidate_count, processed_count=processed_count, error=error, db_path=db_path)


def mark_extraction_failed(note_id: str, space_id: str, *, error: str, db_path: Any = None) -> MemoryExtractionState:
    return _mark_extraction_state(note_id, space_id, "failed", error=error, db_path=db_path)


def list_retryable_extraction_states(space_id: str, *, limit: int = 100, db_path: Any = None) -> list[MemoryExtractionState]:
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
    cadence = cadence.strip().lower()
    if cadence == "daily":
        return day.isoformat()
    if cadence == "weekly":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    if cadence == "monthly":
        return f"{day.year:04d}-{day.month:02d}"
    raise ValueError(f"unknown memory consolidation cadence: {cadence}")


def _stale(value: str | None) -> bool:
    if not value:
        return True
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return (datetime.now().astimezone() - parsed).total_seconds() > MEMORY_CONSOLIDATION_RUN_LEASE_SECONDS


def reserve_consolidation_run(space_id: str, cadence: str, period_key: str, db_path: Any = None) -> ConsolidationRun | None:
    del db_path
    cadence = cadence.strip().lower()
    with session_scope() as session:
        ensure_tenant_space(session, space_id)
        old = session.execute(
            select(MemoryConsolidationRun)
            .where(MemoryConsolidationRun.space_id == space_id, MemoryConsolidationRun.cadence == cadence, MemoryConsolidationRun.period_key == period_key)
            .with_for_update()
        ).scalar_one_or_none()
        if old is not None and (old.status == "completed" or (old.status == "running" and not _stale(old.started_at))):
            return None
        values = {"status": "running", "started_at": utc_now_iso(), "completed_at": None, "error": None, "result_json": None}
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
    del db_path
    with session_scope() as session:
        row = session.get(MemoryConsolidationRun, run_id)
        return _run(row) if row is not None else None


def mark_consolidation_completed(run_id: str, result: dict[str, Any], db_path: Any = None) -> None:
    del db_path
    with session_scope() as session:
        row = session.execute(select(MemoryConsolidationRun).where(MemoryConsolidationRun.id == run_id).with_for_update()).scalar_one_or_none()
        if row is not None:
            row.status, row.completed_at, row.error, row.result_json = "completed", utc_now_iso(), None, result


def mark_consolidation_failed(run_id: str, error: str, db_path: Any = None) -> None:
    del db_path
    with session_scope() as session:
        row = session.execute(select(MemoryConsolidationRun).where(MemoryConsolidationRun.id == run_id).with_for_update()).scalar_one_or_none()
        if row is not None:
            row.status, row.completed_at, row.error = "failed", utc_now_iso(), error


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
    from memory.retriever import score_memory
    candidates = list_memories(space_id, status=None if include_inactive else "active", memory_type=memory_type, limit=100, db_path=db_path)
    scored = sorted(((item, score_memory(query, item)) for item in candidates), key=lambda item: item[1], reverse=True)
    limited = [(item, score) for item, score in scored if score >= min_score][: max(1, min(int(limit), 50))]
    if mark_access:
        mark_accessed([item.id for item, _ in limited])
    return limited


def stats(space_id: str, db_path: Any = None) -> dict[str, Any]:
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
    del db_path
    from sqlalchemy import inspect
    from infrastructure.database import get_engine
    return set(inspect(get_engine()).get_table_names())
