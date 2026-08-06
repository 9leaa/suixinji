"""文件作用：单候选编排。

项目关系：本文件依赖 `core`、`core.settings`、`memory.adjudicator`、`memory.advisory` 等 13 个模块；被 `memory.scheduler`、`memory.service`、`tests.test_memory_consolidation`、`tests.test_memory_consolidator_resilience` 等 6 个模块。
"""



from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from json import dumps
import re
from datetime import datetime
from typing import Any

from core import settings
from core.settings import MEMORY_EXTRACTION_LEASE_SECONDS
from memory.adjudicator import adjudicate_memory
from memory.advisory import maybe_memory_relation_advisory
from memory.candidate_retriever import retrieval_signals, retrieve_candidates
from memory.evolution import evolve_memory
from memory.canonicalizer import canonicalize_candidate, task_identity_compatible
from memory.models import MemoryCandidate, MemoryDecision, MemoryRecord, normalize_content, utc_now_iso
from memory.repository import add_memory_relation, add_source, get_extraction_state, list_memories, mark_extraction_failed, update_memory
from memory.shadow import build_relation_shadow_report
from memory.retriever import score_memory
from memory.trace import add_step
from storage.note_storage import is_note_queryable, load_index

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TaskMatch:
    memory: MemoryRecord
    kind: str
    score: float


def _task_topic(value: MemoryCandidate | MemoryRecord) -> str:
    scope = dict(getattr(value, "scope", {}) or {})
    candidates = [
        scope.get("canonical_topic"),
        getattr(value, "predicate", None),
        getattr(value, "object_value", None),
        getattr(value, "content", None),
    ]
    normalized = [normalize_content(str(item)) for item in candidates if str(item or "").strip()]
    return max(normalized, key=len, default="")


def _task_topic_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left = left.replace("的", "")
    right = right.replace("的", "")
    if left == right or left.endswith(right) or right.endswith(left):
        return 1.0
    left_set, right_set = set(left), set(right)
    return len(left_set & right_set) / len(left_set | right_set) if left_set and right_set else 0.0


def _task_match_kind(candidate: MemoryCandidate, memory: MemoryRecord) -> tuple[str, float] | None:
    if candidate.memory_type != "task" or memory.memory_type != "task":
        return None
    candidate_scope = normalize_content(str(candidate.scope.get("scope") or "global"))
    memory_scope = normalize_content(str(memory.scope.get("scope") or "global"))
    if candidate_scope != memory_scope:
        return None
    if candidate.effective_memory_key == memory.effective_memory_key:
        return "exact_key", 1.0
    structured = (
        normalize_content(candidate.subject or "") == normalize_content(memory.subject or "")
        and normalize_content(candidate.predicate or "") == normalize_content(memory.predicate or "")
        and normalize_content(str(candidate.scope.get("operation") or "")) == normalize_content(str(memory.scope.get("operation") or ""))
    )
    if structured and candidate.subject and candidate.predicate:
        return "structured_identity", 0.98
    if task_identity_compatible(candidate, memory):
        return "identity_compatible", 0.92
    if normalize_content(candidate.subject or "") == normalize_content(memory.subject or ""):
        candidate_attribute = normalize_content(candidate.predicate or "").replace("的", "")
        memory_attribute = normalize_content(memory.predicate or "").replace("的", "")
        if candidate_attribute.endswith(memory_attribute) or memory_attribute.endswith(candidate_attribute):
            # A deterministic attribute suffix is a controlled identity refinement,
            # not a broad fuzzy hit; it may safely advance the same task lifecycle.
            return "identity_compatible", 0.94
        score = _task_topic_similarity(_task_topic(candidate), _task_topic(memory))
        if score >= 0.72:
            return "fuzzy_topic", score
    return None


def _matched_task_memories(candidate: MemoryCandidate, memories: list[MemoryRecord]) -> list[_TaskMatch]:
    matches: list[_TaskMatch] = []
    seen: set[str] = set()
    for memory in memories:
        result = _task_match_kind(candidate, memory)
        if result is None or memory.id in seen:
            continue
        kind, score = result
        seen.add(memory.id)
        matches.append(_TaskMatch(memory=memory, kind=kind, score=score))
    rank = {"exact_key": 4, "structured_identity": 3, "identity_compatible": 2, "fuzzy_topic": 1}
    return sorted(matches, key=lambda item: (rank[item.kind], item.score, item.memory.updated_at), reverse=True)


def find_matching_task_memories(space_id: str, candidate: MemoryCandidate) -> list[MemoryRecord]:
    """Return same-space historical task matches for Stage-2 completion adjudication."""
    memories = list_memories(space_id, status="active", memory_type="task", limit=200)
    return [match.memory for match in _matched_task_memories(candidate, memories)]


def is_strong_task_completion(candidate: MemoryCandidate) -> bool:
    """Detect trackable project/task completions without an online LLM judge."""
    if candidate.memory_type != "task" or candidate.task_status != "done":
        return False
    entity = normalize_content(candidate.subject or "")
    attribute = normalize_content(candidate.predicate or "")
    operation = normalize_content(str(candidate.scope.get("operation") or ""))
    if not entity or entity in {"用户", "我", "本人", "user", "me"}:
        return False
    if not attribute or attribute in {"task", "任务", "待办", "完成状态", "执行", "维护"}:
        return False
    return operation in {
        "修复", "制作", "完成", "完善", "处理", "修改", "实现", "学习", "提交",
        "更换", "部署", "发布", "收集", "设计", "整理", "编写", "搭建", "测试",
    } or len(attribute) >= 4


def _completion_event_topic(candidate: MemoryCandidate) -> str:
    source = str(candidate.evidence_span or candidate.content or "").strip()
    compact = re.sub(r"^(?:今天|昨天|前天|刚才|上周|本周|上午|下午|晚上|最近)", "", source).strip()
    compact = re.sub(r"^(?:我|我们|用户)", "", compact).strip()
    for marker in ("完成", "做完", "搞定", "提交", "恢复", "学完", "弄好", "发布"):
        index = compact.find(marker)
        if index >= 0:
            compact = compact[index:]
            break
    return compact.strip(" ：:，,。！？!?；;") or normalize_content(candidate.object_value or source)


def convert_orphan_done_task_to_episodic(candidate: MemoryCandidate) -> MemoryCandidate:
    """Convert a weak no-history completion claim into a historical event."""
    # The structured extractor already supplied the canonical event identity.
    # Prefer it over a lossy re-parse of natural language (which can retain
    # batch labels, owners, or completion boilerplate).
    topic = str(candidate.scope.get("canonical_topic") or candidate.object_value or _completion_event_topic(candidate)).strip()
    scope = dict(candidate.scope)
    for key in ("operation", "task_status", "old_value"):
        scope.pop(key, None)
    scope.update({"canonical_topic": topic, "new_value": topic, "scope": "history", "derived_from": "orphan_completion"})
    converted = replace(
        candidate,
        memory_type="episodic",
        # Canonicalization derives episodic identity from evidence text first.
        # Give it the trusted structured topic, not the noisy task sentence.
        content=topic,
        evidence_span=topic,
        subject="用户",
        predicate="event",
        object_value=topic,
        task_status=None,
        memory_key=None,
        memory_key_version="memory-key-v3",
        scope=scope,
        reason="orphan_completion_converted_to_event",
    )
    return canonicalize_candidate(converted)


def _done_audit(candidate: MemoryCandidate, matches: list[_TaskMatch], *, relation: str, action: str, reason: str, converted_memory_type: str | None = None) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "note_id": candidate.note_id,
        "matched_memory_ids": [match.memory.id for match in matches],
        "matching_scores": [{"memory_id": match.memory.id, "kind": match.kind, "score": match.score} for match in matches],
        "previous_status": [match.memory.task_status for match in matches],
        "requested_status": candidate.task_status,
        "final_action": action,
        "relation": relation,
        "reason": reason,
        "converted_memory_type": converted_memory_type,
    }


def _done_decision(candidate: MemoryCandidate, matches: list[_TaskMatch], *, relation: str, action: str, reason: str, converted_memory_type: str | None = None) -> MemoryDecision:
    audit = _done_audit(candidate, matches, relation=relation, action=action, reason=reason, converted_memory_type=converted_memory_type)
    evidence = [f"note:{candidate.note_id}", f"audit:{dumps(audit, ensure_ascii=False, sort_keys=True)}"]
    evidence.extend(f"memory:{match.memory.id}" for match in matches)
    return MemoryDecision(
        candidate_id=candidate.candidate_id,
        relation=relation,
        target_memory_ids=[match.memory.id for match in matches],
        confidence=max(0.8, float(candidate.confidence)),
        reason=reason,
        evidence=evidence,
        recommended_action=action,
        target_snapshot_version=matches[0].memory.current_version if len(matches) == 1 else None,
    )


def update_existing_task_to_done(candidate: MemoryCandidate, existing: MemoryRecord, *, match: _TaskMatch | None = None) -> MemoryDecision:
    return _done_decision(candidate, [match or _TaskMatch(existing, "exact_key", 1.0)], relation="update_task", action="update_task", reason="completion_claim", converted_memory_type=None)


def add_source_or_noop(candidate: MemoryCandidate, existing: MemoryRecord, *, match: _TaskMatch | None = None) -> MemoryDecision:
    return _done_decision(candidate, [match or _TaskMatch(existing, "exact_key", 1.0)], relation="same", action="add_source", reason="duplicate_done_completion", converted_memory_type=None)


def consolidate_done_task(space_id: str, note_id: str, candidate: MemoryCandidate, *, trace: dict[str, Any] | None = None) -> dict[str, Any]:
    """Legacy entry point retained for callers; all task states use one path."""
    return _consolidate_candidate_standard(space_id, note_id, candidate, trace=trace)


def _legacy_consolidate_done_task(space_id: str, note_id: str, candidate: MemoryCandidate, *, trace: dict[str, Any] | None = None) -> dict[str, Any]:
    """Previous specialized implementation, kept temporarily for audit history."""
    history = list_memories(space_id, status="active", memory_type="task", limit=200)
    matches = _matched_task_memories(candidate, history)
    add_step(
        trace,
        "done_task_history_checked",
        input_summary={"candidate_id": candidate.candidate_id, "requested_status": candidate.task_status},
        output_summary={
            "matched_memory_ids": [match.memory.id for match in matches],
            "matching_scores": [{"memory_id": match.memory.id, "kind": match.kind, "score": match.score} for match in matches],
        },
    )
    candidate_for_evolution = candidate
    if len(matches) > 1:
        decision = _done_decision(candidate, matches, relation="ambiguous_match", action="pending_review", reason="ambiguous_task_match")
    elif not matches:
        if is_strong_task_completion(candidate):
            decision = _done_decision(candidate, [], relation="orphan_completion", action="pending_review", reason="strong_task_identity_without_history")
        else:
            candidate_for_evolution = convert_orphan_done_task_to_episodic(candidate)
            decision = _done_decision(candidate, [], relation="orphan_completion", action="insert", reason="orphan_completion_converted_to_event", converted_memory_type="episodic")
    else:
        match = matches[0]
        existing = match.memory
        if match.kind == "fuzzy_topic":
            decision = _done_decision(candidate, matches, relation="ambiguous_match", action="pending_review", reason="fuzzy_task_match_requires_review")
        elif existing.task_status == "todo":
            decision = update_existing_task_to_done(candidate, existing, match=match)
        elif existing.task_status == "done":
            decision = add_source_or_noop(candidate, existing, match=match)
        else:
            decision = _done_decision(candidate, matches, relation="conflict", action="pending_review", reason="unsupported_previous_task_status")
    add_step(
        trace,
        "relation_decided",
        input_summary={"candidate_id": candidate.candidate_id},
        output_summary={
            "decision_id": decision.decision_id,
            "relation": decision.relation,
            "target_memory_ids": decision.target_memory_ids,
            "action": decision.recommended_action,
            "audit": decision.evidence[1] if len(decision.evidence) > 1 else None,
        },
        reason=decision.reason,
    )
    # A completion changes lifecycle state, not task identity.  Keep the
    # historical canonical key on a todo -> done transition even
    # when the extractor supplied a more specific wording in this note.
    if decision.recommended_action == "update_task" and len(matches) == 1:
        existing = matches[0].memory
        candidate_for_evolution = replace(
            candidate_for_evolution,
            memory_key=existing.memory_key,
            memory_key_version=existing.memory_key_version,
        )
    result = evolve_memory(
        space_id=space_id,
        note_id=note_id,
        candidate=candidate_for_evolution,
        decision=decision,
        trace=trace,
    )
    antecedent_note_id = str(candidate.scope.get("antecedent_note_id") or "")
    result_memory_id = str(result.get("memory_id") or "")
    if candidate.scope.get("reference_status") == "resolved" and antecedent_note_id and result_memory_id:
        add_source(result_memory_id, antecedent_note_id, "supported_by")
        add_step(
            trace,
            "antecedent_source_linked",
            output_summary={"memory_id": result_memory_id, "antecedent_note_id": antecedent_note_id},
            reason="resolved_reference_identity_evidence",
        )
    return result


def _is_processing_stale(updated_at: str | None) -> bool:
    """函数功能：`_is_processing_stale` 负责判断是否为 processing stale，服务于本文件职责：单候选编排。
    传参：
        updated_at: updated at 参数，由调用方传入，类型为 `str | None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if not updated_at:
        return True
    try:
        parsed = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return (datetime.now().astimezone() - parsed).total_seconds() > MEMORY_EXTRACTION_LEASE_SECONDS


def _consolidate_candidate_standard(space_id: str, note_id: str, candidate: MemoryCandidate, *, trace: dict[str, Any] | None = None) -> dict[str, Any]:
    """函数功能：`consolidate_candidate` 负责合并长期记忆 candidate，服务于本文件职责：单候选编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        trace: trace 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    add_step(
        trace,
        "retrieval_started",
        input_summary={"candidate_id": candidate.candidate_id, "memory_type": candidate.memory_type},
    )
    retrieval_started = time.perf_counter()
    similar = retrieve_candidates(space_id, candidate)
    add_step(
        trace,
        "candidate_memories_found",
        input_summary={"candidate_id": candidate.candidate_id, "memory_type": candidate.memory_type},
        output_summary={
            "retrieved_count": len(similar),
            "memory_ids": [memory.id for memory in similar],
            "signals": [retrieval_signals(candidate, memory) for memory in similar[:8]],
        },
        duration_ms=int((time.perf_counter() - retrieval_started) * 1000),
    )
    adjudication_started = time.perf_counter()
    decision = adjudicate_memory(candidate, similar)
    advisory = maybe_memory_relation_advisory(candidate, similar, decision)
    shadow = build_relation_shadow_report(candidate, similar, decision)
    if shadow is not None:
        add_step(
            trace,
            "v3_shadow_relation_evaluated",
            output_summary=shadow,
            reason="read_only_relation_guard_projection",
        )
    add_step(
        trace,
        "relation_decided",
        input_summary={"candidate_id": candidate.candidate_id},
        output_summary={
            "decision_id": decision.decision_id,
            "relation": decision.relation,
            "target_memory_ids": decision.target_memory_ids,
            "action": decision.recommended_action,
            "confidence": decision.confidence,
            "strong_advisory": advisory,
        },
        duration_ms=int((time.perf_counter() - adjudication_started) * 1000),
        reason=decision.reason,
    )
    candidate_for_evolution = candidate
    if decision.recommended_action in {"update_task", "update", "merge"} and len(decision.target_memory_ids) == 1:
        target = next((memory for memory in similar if memory.id == decision.target_memory_ids[0]), None)
        if target is not None:
            candidate_for_evolution = replace(
                candidate,
                memory_key=target.memory_key,
                memory_key_version=target.memory_key_version,
            )
    result = evolve_memory(
        space_id=space_id,
        note_id=note_id,
        candidate=candidate_for_evolution,
        decision=decision,
        trace=trace,
    )
    antecedent_note_id = str(candidate.scope.get("antecedent_note_id") or "")
    result_memory_id = str(result.get("memory_id") or "")
    if candidate.scope.get("reference_status") == "resolved" and antecedent_note_id and result_memory_id:
        add_source(result_memory_id, antecedent_note_id, "supported_by")
        add_step(
            trace,
            "antecedent_source_linked",
            output_summary={"memory_id": result_memory_id, "antecedent_note_id": antecedent_note_id},
            reason="resolved_reference_identity_evidence",
        )
    return result


def consolidate_candidate(space_id: str, note_id: str, candidate: MemoryCandidate, *, trace: dict[str, Any] | None = None) -> dict[str, Any]:
    """Consolidate every candidate through the same retrieval/adjudication path."""
    return _consolidate_candidate_standard(space_id, note_id, candidate, trace=trace)


def process_unextracted_notes(space_id: str, *, limit: int = 100) -> dict[str, Any]:
    """函数功能：`process_unextracted_notes` 负责处理 unextracted notes，服务于本文件职责：单候选编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `100`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    from memory.service import process_note_memory

    processed = []
    failed = []
    skipped = 0
    for note in load_index(space_id)[: max(1, min(int(limit), 500))]:
        if not is_note_queryable(note):
            skipped += 1
            continue
        note_id = str(note.get("id") or "")
        if not note_id:
            skipped += 1
            continue
        state = get_extraction_state(note_id)
        if state is not None and state.status in {"completed", "empty"}:
            skipped += 1
            continue
        if state is not None and state.status == "processing":
            if not _is_processing_stale(state.updated_at):
                skipped += 1
                continue
            mark_extraction_failed(note_id, space_id, error="stale processing lease expired")
        try:
            report = process_note_memory(note)
            processed.append(
                {
                    "note_id": note_id,
                    "trace_id": report.get("trace_id"),
                    "candidates": report.get("candidates"),
                    "extraction_status": report.get("extraction_status"),
                }
            )
        except Exception as exc:
            LOGGER.exception(
                "memory.daily.note.failed space_id=%s note_id=%s error_type=%s",
                space_id,
                note_id,
                type(exc).__name__,
            )
            failed.append({"note_id": note_id, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "space_id": space_id,
        "processed": processed,
        "failed": failed,
        "processed_count": len(processed),
        "failed_count": len(failed),
        "skipped_count": skipped,
        "status": "partial" if failed else "completed",
    }


def merge_duplicate_episodic(space_id: str, *, min_score: float = 0.72) -> dict[str, Any]:
    """函数功能：`merge_duplicate_episodic` 负责合并 duplicate episodic，服务于本文件职责：单候选编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        min_score: min score 参数，由调用方传入，类型为 `float`，默认值为 `0.72`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    episodic = list_memories(space_id, status="active", memory_type="episodic", limit=100)
    merged: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for keeper in episodic:
        if keeper.id in consumed:
            continue
        for duplicate in episodic:
            if duplicate.id == keeper.id or duplicate.id in consumed:
                continue
            score = score_memory(keeper.content, duplicate)
            if score < min_score:
                continue
            for source in duplicate.sources:
                add_source(keeper.id, source.note_id, "supported_by")
            update_memory(duplicate.id, status="superseded", valid_until=utc_now_iso(), reason="weekly_duplicate_episodic_merge")
            consumed.add(duplicate.id)
            merged.append({"keeper_id": keeper.id, "merged_id": duplicate.id, "score": score})
    return {"space_id": space_id, "merged_count": len(merged), "merged": merged}


def generate_stable_semantic(space_id: str, *, min_sources: int = 3) -> dict[str, Any]:
    """函数功能：`generate_stable_semantic` 负责生成 stable semantic，服务于本文件职责：单候选编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        min_sources: min sources 参数，由调用方传入，类型为 `int`，默认值为 `3`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    episodic = list_memories(space_id, status="active", memory_type="episodic", limit=100)
    if len(episodic) < min_sources:
        return {"space_id": space_id, "created": False, "reason": "not_enough_sources", "source_count": len(episodic)}

    # consolidation 保持领域中立：有结构化 predicate 时按其成簇，否则从当前 episodic 流中成簇。
    grouped: dict[str, list[Any]] = {}
    for memory in episodic:
        group_key = memory.predicate or "episodic_stream"
        grouped.setdefault(group_key, []).append(memory)
    cluster = max(grouped.values(), key=lambda items: (len(items), max(item.updated_at for item in items)))
    if len(cluster) < min_sources:
        return {"space_id": space_id, "created": False, "reason": "no_stable_cluster", "source_count": len(cluster)}

    source_note_ids = list(dict.fromkeys(source.note_id for memory in cluster for source in memory.sources))
    source_note_ids = source_note_ids[:100]
    source_contents = list(dict.fromkeys(memory.content for memory in cluster))[:5]
    candidate = MemoryCandidate(
        memory_type="semantic",
        content="用户近期稳定主题：" + "；".join(source_contents),
        importance=0.9,
        confidence=0.82,
        entities=[],
        reason="monthly_generic_cluster_consolidation",
        space_id=space_id,
        subject="用户",
        predicate="stable_theme",
        object_value=cluster[0].predicate or "episodic_stream",
    )
    first_source = source_note_ids[0] if source_note_ids else cluster[0].id
    result = consolidate_candidate(space_id, first_source, candidate)
    memory_id = str(result.get("memory_id") or "")
    if not memory_id:
        return {"space_id": space_id, "created": False, "reason": "candidate_not_applied", "source_count": len(source_note_ids)}
    for note_id in source_note_ids[1:]:
        add_source(memory_id, note_id, "summarized_from")
    for source_memory in cluster:
        add_memory_relation(space_id, memory_id, source_memory.id, "summarized_from", decision_id=result.get("decision_id"))
    return {
        "space_id": space_id,
        "created": result.get("action") == "insert",
        "action": result.get("action"),
        "memory_id": memory_id,
        "source_count": len(source_note_ids),
    }


def run_monthly_semantic_consolidation(space_id: str, *, min_cluster_size: int = 3) -> dict[str, Any]:
    """函数功能：`run_monthly_semantic_consolidation` 负责运行 monthly semantic consolidation，服务于本文件职责：单候选编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        min_cluster_size: min cluster size 参数，由调用方传入，类型为 `int`，默认值为 `3`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    memories = list_memories(space_id, status="active", limit=500)
    groups: dict[tuple[str, str], list[Any]] = {}
    for memory in memories:
        if memory.memory_type == "task":
            continue
        key = memory.effective_memory_key or memory.predicate or memory.normalized_content[:48]
        groups.setdefault((memory.memory_type, key), []).append(memory)

    reviewed = 0
    created = []
    skipped = []
    for (memory_type, key), cluster in groups.items():
        if len(cluster) < min_cluster_size:
            continue
        reviewed += 1
        polarities = {memory.polarity for memory in cluster if memory.polarity}
        if len(polarities) > 1:
            skipped.append({"memory_type": memory_type, "key": key, "reason": "polarity_conflict", "count": len(cluster)})
            continue
        source_note_ids = list(dict.fromkeys(source.note_id for memory in cluster for source in memory.sources))
        if len(source_note_ids) < min_cluster_size:
            skipped.append({"memory_type": memory_type, "key": key, "reason": "not_enough_sources", "count": len(source_note_ids)})
            continue
        contents = list(dict.fromkeys(memory.content for memory in cluster))[:5]
        candidate = MemoryCandidate(
            memory_type="semantic",
            content="用户长期稳定主题：" + "；".join(contents),
            importance=max(memory.importance for memory in cluster),
            confidence=min(0.9, max(memory.confidence for memory in cluster)),
            entities=[],
            reason="monthly_semantic_cluster_consolidation",
            space_id=space_id,
            subject="用户",
            predicate="stable_theme",
            object_value=key[:120],
        )
        result = consolidate_candidate(space_id, source_note_ids[0], candidate)
        memory_id = str(result.get("memory_id") or "")
        if not memory_id:
            skipped.append({"memory_type": memory_type, "key": key, "reason": "candidate_not_applied", "count": len(cluster)})
            continue
        for note_id in source_note_ids[1:]:
            add_source(memory_id, note_id, "summarized_from")
        for source_memory in cluster:
            add_memory_relation(space_id, memory_id, source_memory.id, "summarized_from", decision_id=result.get("decision_id"))
        created.append({"memory_id": memory_id, "memory_type": memory_type, "key": key, "source_count": len(source_note_ids)})

    if not settings.MONTHLY_SEMANTIC_CONSOLIDATION_ENABLED:
        fallback = generate_stable_semantic(space_id, min_sources=min_cluster_size)
        return {"space_id": space_id, "mode": "legacy_fallback", "semantic_reviewed": reviewed, "fallback": fallback}
    return {
        "space_id": space_id,
        "mode": "semantic_cluster",
        "reviewed_clusters": reviewed,
        "created_count": len(created),
        "created": created,
        "skipped": skipped[:50],
    }
