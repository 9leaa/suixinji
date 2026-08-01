#!/usr/bin/env python3
"""文件作用：任务生命周期对账。

项目关系：本文件依赖 `infrastructure.database`、`infrastructure.schema`、`memory.canonicalizer`、`memory.models` 等 6 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from infrastructure.database import session_scope
from infrastructure.schema import Memory, MemoryDecision, MemoryRelation, MemorySource, MemoryVersion, Note, Space
from memory.canonicalizer import task_key
from memory.models import MEMORY_KEY_V3_VERSION, new_id, normalize_content, utc_now_iso
from repositories.postgres.common import parse_datetime
from repositories.postgres.memory import _schedule_memory_embedding_if_enabled


@dataclass(frozen=True)
class LifecyclePoint:
    """类功能：`LifecyclePoint` 封装与“任务生命周期对账”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    note_id: str
    task_status: str
    content: str


def _parse_point(value: str) -> LifecyclePoint:
    """函数功能：`_parse_point` 负责解析 point，服务于本文件职责：任务生命周期对账。
    传参：
        value: 待转换、校验或计算的值，类型为 `str`。
    返回结果说明：
        返回 `LifecyclePoint` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    try:
        note_id, status, content = value.split("|", 2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--point format: NOTE_ID|todo|display content") from exc
    if status not in {"todo", "blocked", "done", "cancelled"}:
        raise argparse.ArgumentTypeError(f"invalid task status: {status}")
    if not note_id or not content.strip():
        raise argparse.ArgumentTypeError("point needs a note id and display content")
    return LifecyclePoint(note_id, status, content.strip())


def _report(space_id: str, task_key_value: str, points: list[LifecyclePoint], archived_ids: list[str], memory_id: str | None, *, executed: bool) -> dict[str, Any]:
    """函数功能：`_report` 负责处理 report，服务于本文件职责：任务生命周期对账。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        task_key_value: task key value 参数，由调用方传入，类型为 `str`。
        points: points 参数，由调用方传入，类型为 `list[LifecyclePoint]`。
        archived_ids: archived ids 参数，由调用方传入，类型为 `list[str]`。
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str | None`。
        executed: executed 参数，由调用方传入，类型为 `bool`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    return {
        "mode": "executed" if executed else "dry_run",
        "space_id": space_id,
        "canonical_key": task_key_value,
        "point_count": len(points),
        "source_note_ids": [point.note_id for point in points],
        "final_task_status": points[-1].task_status,
        "repaired_memory_id": memory_id,
        "archived_memory_ids": archived_ids,
    }


def reconcile_task_lifecycle(
    *,
    space_id: str,
    entity: str,
    attribute: str,
    operation: str,
    canonical_topic: str,
    points: list[LifecyclePoint],
    execute: bool = False,
) -> dict[str, Any]:
    """函数功能：`reconcile_task_lifecycle` 负责对账 task lifecycle，服务于本文件职责：任务生命周期对账。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        entity: entity 参数，由调用方传入，类型为 `str`。
        attribute: attribute 参数，由调用方传入，类型为 `str`。
        operation: operation 参数，由调用方传入，类型为 `str`。
        canonical_topic: canonical topic 参数，由调用方传入，类型为 `str`。
        points: points 参数，由调用方传入，类型为 `list[LifecyclePoint]`。
        execute: execute 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    if len(points) < 2:
        raise ValueError("at least two reviewed lifecycle points are required")
    if len({point.note_id for point in points}) != len(points):
        raise ValueError("each lifecycle point must reference a different note")
    key = task_key(entity, attribute, operation, "global")
    with session_scope() as session:
        space = session.execute(select(Space).where(Space.id == space_id).with_for_update()).scalar_one_or_none()
        if space is None:
            raise ValueError("space not found")
        notes = list(session.execute(select(Note).where(Note.space_id == space_id, Note.id.in_([point.note_id for point in points]))).scalars())
        notes_by_id = {note.id: note for note in notes}
        missing = [point.note_id for point in points if point.note_id not in notes_by_id]
        if missing:
            raise ValueError(f"reviewed source notes not found in space: {','.join(missing)}")

        existing_correct = session.execute(
            select(Memory).where(Memory.space_id == space_id, Memory.status == "active", Memory.memory_key == key).with_for_update()
        ).scalar_one_or_none()
        source_rows = list(
            session.execute(
                select(MemorySource).where(MemorySource.note_id.in_([point.note_id for point in points]))
            ).scalars()
        )
        split_rows = list(
            session.execute(
                select(Memory)
                .where(
                    Memory.space_id == space_id,
                    Memory.memory_type == "task",
                    Memory.status == "active",
                    Memory.id.in_([row.memory_id for row in source_rows]),
                )
                .with_for_update()
            ).scalars()
        ) if source_rows else []

        if existing_correct is not None:
            correct_sources = {
                row.note_id
                for row in session.execute(select(MemorySource).where(MemorySource.memory_id == existing_correct.id)).scalars()
            }
            if {point.note_id for point in points}.issubset(correct_sources) and existing_correct.task_status == points[-1].task_status:
                return _report(space_id, key, points, [], existing_correct.id, executed=False) | {"already_reconciled": True}

        if not execute:
            return _report(space_id, key, points, [row.id for row in split_rows], existing_correct.id if existing_correct else None, executed=False)

        now = parse_datetime(utc_now_iso())
        target = existing_correct
        if target is None:
            target = Memory(
                id=new_id("mem"),
                tenant_id=space.tenant_id,
                space_id=space_id,
                memory_type="task",
                content=points[-1].content,
                normalized_content=normalize_content(points[-1].content),
                importance=max((float(row.importance) for row in split_rows), default=0.8),
                confidence=max((float(row.confidence) for row in split_rows), default=0.9),
                status="active",
                task_status=points[-1].task_status,
                subject=entity,
                predicate=attribute,
                object_value=attribute,
                memory_key=key,
                memory_key_version=MEMORY_KEY_V3_VERSION,
                polarity=None,
                scope_json={
                    "canonical_topic": canonical_topic,
                    "operation": operation,
                    "scope": "global",
                    "memory_key_version": MEMORY_KEY_V3_VERSION,
                    "repaired": True,
                },
                valid_from=notes_by_id[points[0].note_id].created_at,
                valid_until=None,
                last_confirmed_at=notes_by_id[points[-1].note_id].created_at,
                created_at=now,
                updated_at=now,
                current_version=len(points),
            )
            session.add(target)
            session.flush()
            for version, point in enumerate(points, start=1):
                note = notes_by_id[point.note_id]
                session.add(
                    MemoryVersion(
                        id=new_id("ver"),
                        memory_id=target.id,
                        version=version,
                        content=point.content,
                        status="active",
                        task_status=point.task_status,
                        confidence=target.confidence,
                        importance=target.importance,
                        valid_from=note.created_at,
                        valid_until=None,
                        reason="explicit_task_lifecycle_repair",
                        source_note_id=point.note_id,
                        created_at=now,
                    )
                )
        else:
            target.content = points[-1].content
            target.task_status = points[-1].task_status
            target.updated_at = now

        for index, point in enumerate(points):
            session.add(
                MemorySource(
                    memory_id=target.id,
                    note_id=point.note_id,
                    relation="created_from" if index == 0 else "updated_by",
                    created_at=notes_by_id[point.note_id].created_at,
                )
            )

        archived_ids: list[str] = []
        for row in split_rows:
            if row.id == target.id:
                continue
            row.status = "archived"
            row.updated_at = now
            row.current_version += 1
            session.add(
                MemoryVersion(
                    id=new_id("ver"), memory_id=row.id, version=row.current_version,
                    content=row.content, status="archived", task_status=row.task_status,
                    confidence=row.confidence, importance=row.importance,
                    valid_from=row.valid_from, valid_until=row.valid_until,
                    reason="superseded_by_explicit_task_lifecycle_repair", source_note_id=None, created_at=now,
                )
            )
            session.add(
                MemoryRelation(
                    id=new_id("rel"), space_id=space_id, source_memory_id=target.id, target_memory_id=row.id,
                    relation="derived_from", decision_id=None, created_at=now,
                )
            )
            archived_ids.append(row.id)

        decision = MemoryDecision(
            id=new_id("decision"), space_id=space_id, note_id=points[-1].note_id,
            candidate_id=f"repair:{target.id}", relation="merge", target_memory_ids_json=[row.id for row in split_rows],
            confidence=1.0, reason="explicit_task_lifecycle_repair", evidence_json=[f"note:{point.note_id}" for point in points],
            recommended_action="merge", status="applied", result_memory_ids_json=[target.id, *archived_ids], error=None,
            policy_version="memory-v3-repair-v1", adjudicator_version="human_reviewed", model=None, prompt_hash=None,
            input_hash=None, target_snapshot_version=None, retry_of_decision_id=None, created_at=now, applied_at=now,
        )
        session.add(decision)
        _schedule_memory_embedding_if_enabled(session, target, force=True)
        return _report(space_id, key, points, archived_ids, target.id, executed=True)


def main() -> int:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：任务生命周期对账。
    传参：
        无。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    parser = argparse.ArgumentParser(description="Reconcile an explicitly reviewed split task lifecycle")
    parser.add_argument("--space-id", required=True)
    parser.add_argument("--entity", required=True)
    parser.add_argument("--attribute", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--canonical-topic", required=True)
    parser.add_argument("--point", type=_parse_point, action="append", required=True, help="NOTE_ID|task_status|display content; repeat in chronological order")
    parser.add_argument("--execute", action="store_true", help="apply the reviewed repair; default is dry-run")
    args = parser.parse_args()
    report = reconcile_task_lifecycle(
        space_id=args.space_id,
        entity=args.entity,
        attribute=args.attribute,
        operation=args.operation,
        canonical_topic=args.canonical_topic,
        points=list(args.point),
        execute=args.execute,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
