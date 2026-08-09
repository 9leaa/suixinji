"""文件作用：Memory 公共服务与飞书命令格式化。

项目关系：本文件依赖 `agent.hooks`、`core.sensitive`、`core.settings`、`infrastructure.redis_keys` 等 14 个模块；被 `agent.query_agent`、`apps.handlers`、`bot.feishu_bot`、`core.worker` 等 16 个模块。
"""



from __future__ import annotations

import logging
import re
from dataclasses import asdict, is_dataclass, replace
from datetime import date
from typing import Any

from agent.hooks import AgentRunContext, get_default_hook_manager
from core.sensitive import safe_text_preview
from core.settings import MEMORY_EXTRACTOR_MODE, MEMORY_QUERY_MIN_SCORE
from infrastructure.redis_keys import KEYS
from infrastructure.redis_lock import coordinated_lock
from memory.consolidator import consolidate_candidate
from memory.candidate_validator import contains_sensitive_data, validate_candidates
from memory.canonicalizer import task_instance_authorized
from memory.extractor import extract_candidates, may_contain_memory
from memory.models import candidate_id_for, candidate_id_for_evidence
from memory.shadow import build_shadow_report
from memory.repository import (
    approve_pending_memory,
    correct_memory,
    edit_pending_memory,
    get_extraction_state,
    get_memory_candidate,
    get_memory_candidate_status,
    get_memory,
    list_memories,
    list_memory_decisions,
    list_memory_relations,
    mark_extraction_completed,
    mark_extraction_empty,
    mark_extraction_empty_attempt,
    mark_extraction_failed,
    mark_extraction_partial,
    mark_extraction_processing,
    mark_memory_candidate,
    purge_memory,
    reject_pending_memory,
    resolve_memory_conflict,
    save_memory_candidate,
    search_memories,
    soft_delete_memory,
    stats,
)
from memory.scheduler import run_memory_consolidation_once
from memory.task_state import infer_task_status
from memory.trace import add_step, find_traces_by_memory, finish_trace, get_trace, latest_trace, start_trace


LOGGER = logging.getLogger(__name__)


_COVERAGE_QUERY_MARKERS = ("列出", "列举", "分别", "概括", "汇总", "有哪些", "哪几", "几件", "几个")


def _coverage_identity(item: dict[str, Any]) -> str:
    """Return a stable business identity for diversity, never an evaluator id."""
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    return str(
        scope.get("task_family_key")
        or scope.get("preference_family_key")
        or scope.get("canonical_topic")
        or item.get("canonical_topic")
        or item.get("memory_key")
        or item.get("predicate")
        or item.get("object_value")
        or item.get("id")
        or ""
    )


def _has_structured_task_state(item: dict[str, Any]) -> bool:
    """Prefer a task record whose stored value explicitly represents its state."""
    status = str(item.get("task_status") or "").casefold()
    if not status:
        return False
    value = str(item.get("object_value") or item.get("current_value") or "").casefold()
    if value == status:
        return True
    text = " ".join(str(item.get(key) or "") for key in ("content", "memory_key"))
    return bool(re.search(r"(?:[:：=]|状态(?:是|为)?|当前(?:状态)?(?:是|为)?)\s*" + re.escape(status) + r"\b", text, re.IGNORECASE))


def _coverage_rerank_memory_results(
    results: list[dict[str, Any]],
    *,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Diversify inventory-style retrieval with business fields only.

    Ordinary fact questions retain repository ranking.  For an explicit list or
    summary request, a single high-scoring distractor must not crowd every
    independently useful item out of a bounded result set.  ACL filtering has
    already happened before this function; it only changes ordering.
    """
    if not results or not any(marker in str(query or "") for marker in _COVERAGE_QUERY_MARKERS):
        return results[: max(1, int(limit))]

    wants_tasks = any(marker in str(query or "") for marker in ("项目", "任务", "状态"))
    wants_episodes = any(marker in str(query or "") for marker in ("经历", "事件", "最近记录", "几件"))
    candidates = list(results)
    selected: list[dict[str, Any]] = []
    used_identities: set[str] = set()
    used_types: set[str] = set()
    target = max(1, int(limit))

    while candidates and len(selected) < target:
        def rank(item: dict[str, Any]) -> tuple[float, float, str]:
            score = float(item.get("score") or item.get("retrieval_fusion_score") or 0.0)
            identity = _coverage_identity(item)
            memory_type = str(item.get("memory_type") or "")
            coverage_bonus = 0.0 if identity in used_identities else 0.08
            type_bonus = 0.05 if memory_type and memory_type not in used_types else 0.0
            task_bonus = 0.16 if wants_tasks and memory_type == "task" and _has_structured_task_state(item) else 0.0
            episode_bonus = 0.16 if wants_episodes and memory_type == "episodic" else 0.0
            # Preserve repository score as the dominant signal; bonuses only
            # resolve the bounded-list crowding failure mode.
            return (score + coverage_bonus + type_bonus + task_bonus + episode_bonus, score, str(item.get("id") or ""))

        best = max(candidates, key=rank)
        candidates.remove(best)
        selected.append(best)
        identity = _coverage_identity(best)
        if identity:
            used_identities.add(identity)
        if best.get("memory_type"):
            used_types.add(str(best["memory_type"]))
    return selected


def _note_value(note: Any, key: str, default: Any = None) -> Any:
    """函数功能：`_note_value` 负责处理 note value，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        note: note 参数，由调用方传入，类型为 `Any`。
        key: key 参数，由调用方传入，类型为 `str`。
        default: default 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if is_dataclass(note):
        return asdict(note).get(key, default)
    if isinstance(note, dict):
        return note.get(key, default)
    return getattr(note, key, default)


def _process_note_memory_impl(note: Any, classification: dict[str, Any] | None = None) -> dict[str, Any]:
    """函数功能：`_process_note_memory_impl` 负责处理 note memory impl，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        note: note 参数，由调用方传入，类型为 `Any`。
        classification: classification 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    note_id = str(_note_value(note, "id", ""))
    space_id = str(_note_value(note, "space_id", ""))
    tenant_id = str(_note_value(note, "tenant_id", "default") or "default")
    text = str(_note_value(note, "text", "") or "")

    trace = start_trace("memory_write", space_id, note_id=note_id)
    add_step(trace, "note_saved", output_summary={"note_id": note_id, "text_len": len(text)})
    add_step(
        trace,
        "memory_extraction_started",
        input_summary={"note_id": note_id, "text_len": len(text), "extractor_mode": MEMORY_EXTRACTOR_MODE},
    )

    try:
        if contains_sensitive_data(text):
            state = mark_extraction_empty(note_id, space_id)
            add_step(
                trace,
                "memory_extraction_skipped",
                status="discarded",
                output_summary={"note_id": note_id, "existing_status": state.status},
                reason="sensitive_data",
            )
            finish_trace(trace)
            return {
                "note_id": note_id,
                "space_id": space_id,
                "candidates": 0,
                "results": [],
                "trace_id": trace["trace_id"],
                "extraction_status": "empty",
            }
        existing_state = get_extraction_state(note_id) if note_id else None
        if existing_state is not None and existing_state.status in {"completed", "empty"}:
            add_step(
                trace,
                "memory_extraction_skipped",
                output_summary={"note_id": note_id, "existing_status": existing_state.status},
                reason="terminal_extraction_state",
            )
            finish_trace(trace)
            return {
                "note_id": note_id,
                "space_id": space_id,
                "candidates": existing_state.candidate_count,
                "results": [],
                "trace_id": trace["trace_id"],
                "extraction_status": existing_state.status,
                "idempotent": True,
            }
        if MEMORY_EXTRACTOR_MODE == "rules":
            if not may_contain_memory(text, classification=classification):
                state = mark_extraction_empty_attempt(note_id, space_id)
                add_step(
                    trace,
                    "extraction_state_empty",
                    output_summary={
                        "note_id": note_id,
                        "candidate_count": state.candidate_count,
                        "processed_count": state.processed_count,
                        "attempt_count": state.attempt_count,
                    },
                    reason="deterministic_rules_fast_path",
                )
                add_step(
                    trace,
                    "vector_written",
                    output_summary={"note_id": note_id, "memory_count": 0},
                    reason="note_vector_written_before_memory",
                )
                finish_trace(trace)
                return {
                    "note_id": note_id,
                    "space_id": space_id,
                    "candidates": 0,
                    "results": [],
                    "trace_id": trace["trace_id"],
                    "extraction_status": "empty",
                }
        state = mark_extraction_processing(note_id, space_id)
        add_step(
            trace,
            "extraction_state_processing",
            output_summary={"note_id": note_id, "attempt_count": state.attempt_count},
        )
        previous_messages = list(_note_value(note, "previous_messages", []) or [])[:3]
        extraction_kwargs: dict[str, Any] = {"classification": classification}
        if previous_messages:
            extraction_kwargs["previous_messages"] = previous_messages
        extracted_candidates = extract_candidates(note_id, text, **extraction_kwargs)
        shadow_report = build_shadow_report(extracted_candidates)
        if shadow_report is not None:
            add_step(
                trace,
                "v3_shadow_evaluated",
                output_summary=shadow_report,
                reason="read_only_canonical_identity_projection",
            )
        enriched_candidates = [
            replace(
                candidate,
                note_id=note_id,
                space_id=space_id,
                candidate_id=(
                    candidate.candidate_id
                    if candidate.note_id == note_id
                    else candidate_id_for_evidence(
                        note_id,
                        candidate.memory_type,
                        candidate.content,
                        memory_key=candidate.effective_memory_key,
                        evidence_span=candidate.evidence_span,
                        clause_index=candidate.clause_index,
                    )
                    if candidate.clause_index is not None
                    else candidate_id_for(note_id, candidate.memory_type, candidate.content)
                ),
            )
            for candidate in extracted_candidates
        ]
        for candidate in enriched_candidates:
            save_memory_candidate(candidate, space_id=space_id, status="extracted")
        # 校验前先持久化原始候选，确保被拒绝的模型输出仍能按模型、prompt 和拒绝原因审计。
        for candidate in enriched_candidates:
            add_step(
                trace,
                "candidate_extracted",
                output_summary={
                    "candidate_id": candidate.candidate_id,
                    "memory_type": candidate.memory_type,
                    "importance": candidate.importance,
                    "confidence": candidate.confidence,
                    "should_store": candidate.should_store,
                    "task_status": candidate.task_status,
                    "clause_index": candidate.clause_index,
                    "content_preview": safe_text_preview(candidate.content, limit=180),
                    "evidence_preview": safe_text_preview(candidate.evidence_span or "", limit=180),
                },
                reason=candidate.effective_reason,
            )
        candidates, rejections = validate_candidates(enriched_candidates, note_text=text)
        for rejection in rejections:
            mark_memory_candidate(rejection.candidate_id, "discarded", error=rejection.reason)
            add_step(
                trace,
                "candidate_rejected",
                status="discarded",
                output_summary={"candidate_id": rejection.candidate_id},
                reason=rejection.reason,
            )

        if not candidates:
            state = mark_extraction_empty(note_id, space_id)
            add_step(
                trace,
                "extraction_state_empty",
                output_summary={
                    "note_id": note_id,
                    "candidate_count": state.candidate_count,
                    "processed_count": state.processed_count,
                    "attempt_count": state.attempt_count,
                },
            )
            add_step(trace, "vector_written", output_summary={"note_id": note_id, "memory_count": 0}, reason="note_vector_written_before_memory")
            finish_trace(trace)
            return {
                "note_id": note_id,
                "space_id": space_id,
                "candidates": 0,
                "results": [],
                "rejected_candidates": len(rejections),
                "trace_id": trace["trace_id"],
                "extraction_status": "empty",
            }

        results = []
        errors = []
        for candidate in candidates:
            existing_status = get_memory_candidate_status(candidate.candidate_id)
            if existing_status in {"applied", "pending_review", "discarded"}:
                results.append({"candidate_id": candidate.candidate_id, "action": existing_status, "idempotent": True})
                continue
            try:
                mark_memory_candidate(candidate.candidate_id, "validated")
                mark_memory_candidate(candidate.candidate_id, "processing")
                with coordinated_lock(
                    KEYS.lock_memory_key(tenant_id, space_id, candidate.effective_memory_key),
                    critical=True,
                ):
                    results.append(consolidate_candidate(space_id, note_id, candidate, trace=trace))
                result = results[-1]
                final_status = "pending_review" if result.get("action") == "pending_review" else "discarded" if result.get("action") == "discard" else "applied"
                mark_memory_candidate(candidate.candidate_id, final_status, decision_id=result.get("decision_id"))
            except Exception as exc:
                mark_memory_candidate(candidate.candidate_id, "failed", error=f"{type(exc).__name__}: {exc}")
                errors.append(f"{type(exc).__name__}: {exc}")

        add_step(trace, "vector_written", output_summary={"note_id": note_id, "memory_count": len(results)}, reason="note_vector_written_before_memory")

        if errors and results:
            state = mark_extraction_partial(
                note_id,
                space_id,
                candidate_count=len(candidates),
                processed_count=len(results),
                error="; ".join(errors),
            )
            add_step(
                trace,
                "extraction_state_partial",
                status="partial",
                output_summary={
                    "note_id": note_id,
                    "candidate_count": state.candidate_count,
                    "processed_count": state.processed_count,
                    "attempt_count": state.attempt_count,
                    "error_type": "candidate_error",
                },
            )
            finish_trace(trace, status="partial")
            return {
                "note_id": note_id,
                "space_id": space_id,
                "candidates": len(candidates),
                "results": results,
                "errors": errors,
                "trace_id": trace["trace_id"],
                "extraction_status": "partial",
            }

        if errors:
            error = "; ".join(errors)
            state = mark_extraction_failed(note_id, space_id, error=error)
            add_step(
                trace,
                "extraction_state_failed",
                status="failed",
                output_summary={
                    "note_id": note_id,
                    "candidate_count": len(candidates),
                    "processed_count": 0,
                    "attempt_count": state.attempt_count,
                    "error_type": "candidate_error",
                },
            )
            raise RuntimeError(error)

        state = mark_extraction_completed(note_id, space_id, candidate_count=len(candidates), processed_count=len(results))
        add_step(
            trace,
            "extraction_state_completed",
            output_summary={
                "note_id": note_id,
                "candidate_count": state.candidate_count,
                "processed_count": state.processed_count,
                "attempt_count": state.attempt_count,
            },
        )
        finish_trace(trace)
        return {
            "note_id": note_id,
            "space_id": space_id,
            "candidates": len(candidates),
            "results": results,
            "trace_id": trace["trace_id"],
            "extraction_status": "completed",
        }
    except Exception as exc:
        current = get_extraction_state(note_id)
        if current is None or current.status == "processing":
            state = mark_extraction_failed(note_id, space_id, error=f"{type(exc).__name__}: {exc}")
            add_step(
                trace,
                "extraction_state_failed",
                status="failed",
                output_summary={"note_id": note_id, "attempt_count": state.attempt_count, "error_type": type(exc).__name__},
            )
        add_step(trace, "memory_write_failed", status="failed", error=str(exc))
        finish_trace(trace, status="failed")
        raise


def process_note_memory(note: Any, classification: dict[str, Any] | None = None) -> dict[str, Any]:
    """函数功能：`process_note_memory` 负责处理 note memory，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        note: note 参数，由调用方传入，类型为 `Any`。
        classification: classification 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    note_id = str(_note_value(note, "id", ""))
    space_id = str(_note_value(note, "space_id", ""))
    context = AgentRunContext.create(
        space_id=space_id,
        run_type="memory",
        tenant_id=str(_note_value(note, "tenant_id", "default") or "default"),
        message_id=str(_note_value(note, "message_id", "")) or None,
        task_id=note_id or None,
        metadata={"note_id": note_id},
    )
    manager = get_default_hook_manager()
    return manager.run_agent(
        context,
        lambda: manager.run_tool(
            context,
            "process_memory",
            {"note_id": note_id},
            lambda: _process_note_memory_impl(note, classification),
        ),
    )


def memory_search(
    space_id: str,
    query: str,
    *,
    memory_type: str | None = None,
    min_score: float = MEMORY_QUERY_MIN_SCORE,
    limit: int = 8,
    access_context: Any = None,
) -> list[dict[str, Any]]:
    """函数功能：`memory_search` 负责搜索 memory，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        min_score: min score 参数，由调用方传入，类型为 `float`，默认值为 `MEMORY_QUERY_MIN_SCORE`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `8`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    trace = start_trace("memory_query", space_id, query_len=len(query))
    add_step(trace, "query_received", input_summary={"query_len": len(query), "memory_type": memory_type, "min_score": min_score})
    requested_limit = max(1, min(int(limit), 50))
    fetch_limit = max(30, requested_limit * 4)
    candidates = [
        {**memory.to_dict(), "score": score}
        for memory, score in search_memories(space_id, query, memory_type=memory_type, min_score=min_score, limit=fetch_limit, access_context=access_context)
        if not contains_sensitive_data(memory.content)
    ]
    results = _coverage_rerank_memory_results(candidates, query=query, limit=requested_limit)
    add_step(
        trace,
        "memory_search",
        output_summary={"result_count": len(results), "memory_ids": [item["id"] for item in results]},
    )
    finish_trace(trace)
    return results


def _format_memory(memory: dict[str, Any]) -> str:
    """函数功能：`_format_memory` 负责格式化 memory，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        memory: memory 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    source_count = len(memory.get("sources") or [])
    score = memory.get("score")
    score_text = f"｜score={score}" if score is not None else ""
    return (
        f"- {memory.get('id')}｜{memory.get('memory_type')}｜{memory.get('status')}{score_text}\n"
        f"  {memory.get('content')}\n"
        f"  sources={source_count}｜updated={memory.get('updated_at')}"
    )


def format_memory_list(space_id: str, *, status: str = "active", limit: int = 20) -> str:
    """函数功能：`format_memory_list` 负责格式化 memory list，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'active'`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `20`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    memories = [
        memory.to_dict()
        for memory in list_memories(space_id, status=status, limit=limit)
        if not contains_sensitive_data(memory.content)
    ]
    if not memories:
        return "没有找到长期记忆。"
    return "长期记忆：\n" + "\n".join(_format_memory(memory) for memory in memories)


def format_memory_show(memory_id: str) -> str:
    """函数功能：`format_memory_show` 负责格式化 memory show，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    memory = get_memory(memory_id)
    if memory is None or contains_sensitive_data(memory.content):
        return f"没有找到记忆：{memory_id}"
    data = memory.to_dict()
    lines = [
        f"记忆 {data['id']}：",
        f"- 类型：{data['memory_type']}",
        f"- 状态：{data['status']}",
        f"- 内容：{data['content']}",
        f"- 版本：{data['current_version']}",
        f"- 来源：{len(data['sources'])} 条",
    ]
    for source in data["sources"][:5]:
        lines.append(f"  - {source['relation']} note={source['note_id']}")
    relations = list_memory_relations(memory_id)
    if relations:
        lines.append(f"- 关系：{len(relations)} 条")
        for relation in relations[:6]:
            other_id = relation.target_memory_id if relation.source_memory_id == memory_id else relation.source_memory_id
            lines.append(f"  - {relation.relation} memory={other_id}")
    return "\n".join(lines)


def format_memory_search(space_id: str, query: str) -> str:
    """函数功能：`format_memory_search` 负责格式化 memory search，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    results = memory_search(space_id, query)
    if not results:
        return "没有找到匹配的长期记忆。"
    return "记忆检索结果：\n" + "\n".join(_format_memory(item) for item in results)


def format_memory_forget(memory_id: str) -> str:
    """函数功能：`format_memory_forget` 负责格式化 memory forget，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    existing = get_memory(memory_id)
    if existing is None:
        return f"没有找到记忆：{memory_id}"
    trace = start_trace("memory_control", existing.space_id)
    add_step(trace, "memory_control_requested", input_summary={"memory_id": memory_id, "action": "forget"})
    memory = soft_delete_memory(memory_id)
    if memory is None:
        add_step(trace, "memory_control_failed", status="failed", output_summary={"memory_id": memory_id, "action": "forget"})
        finish_trace(trace, status="failed")
        return f"没有找到记忆：{memory_id}"
    add_step(trace, "memory_forgotten", output_summary={"memory_id": memory_id, "status": memory.status})
    finish_trace(trace)
    return f"已软删除记忆：{memory_id}"


def format_memory_purge(memory_id: str) -> str:
    """函数功能：`format_memory_purge` 负责格式化 memory purge，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    existing = get_memory(memory_id)
    if existing is None:
        return f"没有找到记忆：{memory_id}"
    trace = start_trace("memory_control", existing.space_id)
    add_step(trace, "memory_control_requested", input_summary={"memory_id": memory_id, "action": "purge"})
    if not purge_memory(memory_id):
        add_step(trace, "memory_control_failed", status="failed", output_summary={"memory_id": memory_id, "action": "purge"})
        finish_trace(trace, status="failed")
        return f"没有找到记忆：{memory_id}"
    add_step(trace, "memory_purged", output_summary={"memory_id": memory_id})
    finish_trace(trace)
    return f"已彻底删除记忆：{memory_id}"


def format_memory_correct(memory_id: str, content: str, task_status: str | None = None) -> str:
    """函数功能：`format_memory_correct` 负责格式化 memory correct，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
        task_status: task status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    existing = get_memory(memory_id)
    if existing is None:
        return f"没有找到记忆：{memory_id}"
    trace = start_trace("memory_control", existing.space_id)
    add_step(
        trace,
        "memory_control_requested",
        input_summary={"memory_id": memory_id, "action": "correct", "content_len": len(content)},
    )
    if not content.strip() or contains_sensitive_data(content):
        add_step(
            trace,
            "memory_control_rejected",
            status="rejected",
            output_summary={"memory_id": memory_id, "action": "correct"},
            reason="empty_or_sensitive_content",
        )
        finish_trace(trace, status="rejected")
        return "修正内容为空或包含敏感凭据，未写入长期记忆。"
    try:
        memory = correct_memory(memory_id, content, task_status=task_status)
    except ValueError as exc:
        add_step(trace, "memory_control_rejected", status="rejected", output_summary={"memory_id": memory_id, "action": "correct"}, reason=str(exc))
        finish_trace(trace, status="rejected")
        return f"记忆修正被拒绝：{exc}"
    if memory is None:
        add_step(trace, "memory_control_failed", status="failed", output_summary={"memory_id": memory_id, "action": "correct"})
        finish_trace(trace, status="failed")
        return f"没有找到记忆：{memory_id}"
    add_step(trace, "memory_corrected", output_summary={"memory_id": memory_id, "version": memory.current_version})
    finish_trace(trace)
    status_line = f"\n任务状态：{memory.task_status}" if memory.memory_type == "task" else ""
    return f"已修正记忆：{memory_id}{status_line}\n{memory.content}"


def format_memory_conflicts(space_id: str) -> str:
    """函数功能：`format_memory_conflicts` 负责格式化 memory conflicts，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    memories = [memory.to_dict() for memory in list_memories(space_id, status="conflicted", limit=50)]
    if not memories:
        return "当前没有 conflicted 记忆。"
    return "冲突记忆：\n" + "\n".join(_format_memory(memory) for memory in memories)


def format_memory_pending(space_id: str) -> str:
    """函数功能：`format_memory_pending` 负责格式化 memory pending，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    memories = [memory.to_dict() for memory in list_memories(space_id, status="pending_review", limit=50)]
    if not memories:
        return "当前没有 pending_review 记忆。"
    return "待审记忆：\n" + "\n".join(_format_memory(memory) for memory in memories)


def format_memory_reject(memory_id: str, reason: str = "user_rejected_pending_memory") -> str:
    """函数功能：`format_memory_reject` 负责格式化 memory reject，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        reason: reason 参数，由调用方传入，类型为 `str`，默认值为 `'user_rejected_pending_memory'`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    memory = reject_pending_memory(memory_id, reason=reason)
    if memory is None:
        return f"没有找到待审记忆：{memory_id}"
    return f"已拒绝待审记忆：{memory_id}"


def format_memory_edit(memory_id: str, content: str) -> str:
    """函数功能：`format_memory_edit` 负责格式化 memory edit，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    if contains_sensitive_data(content):
        return "修正内容包含敏感凭据，未写入长期记忆。"
    memory = edit_pending_memory(memory_id, content)
    if memory is None:
        return f"没有找到待审记忆：{memory_id}"
    return f"已编辑并批准记忆：{memory.id}\n{memory.content}"


def format_memory_resolve(memory_id: str, resolution: str, content: str | None = None) -> str:
    """函数功能：`format_memory_resolve` 负责格式化 memory resolve，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        resolution: resolution 参数，由调用方传入，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    memory = resolve_memory_conflict(memory_id, resolution=resolution, content=content)
    if memory is None:
        return f"没有找到冲突记忆：{memory_id}"
    return f"已解决冲突：{memory.id}\n{memory.content}"


def format_memory_approve(memory_id: str) -> str:
    """函数功能：`format_memory_approve` 负责格式化 memory approve，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    existing = get_memory(memory_id)
    if existing is None or existing.status != "pending_review":
        return f"没有找到待审记忆：{memory_id}"
    trace = start_trace("memory_control", existing.space_id)
    add_step(trace, "memory_control_requested", input_summary={"memory_id": memory_id, "action": "approve"})
    memory = approve_pending_memory(memory_id)
    if memory is None:
        add_step(trace, "memory_control_failed", status="failed", output_summary={"memory_id": memory_id, "action": "approve"})
        finish_trace(trace, status="failed")
        return f"没有找到待审记忆：{memory_id}"
    add_step(
        trace,
        "memory_approved",
        output_summary={"memory_id": memory.id, "target_memory_id": memory_id, "status": memory.status},
    )
    finish_trace(trace)
    return f"已批准待审记忆：{memory_id}\n生效记忆：{memory.id}\n{memory.content}"


def format_memory_decisions(space_id: str, *, limit: int = 10) -> str:
    """函数功能：`format_memory_decisions` 负责格式化 memory decisions，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `10`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    decisions = list_memory_decisions(space_id, limit=limit)
    if not decisions:
        return "还没有记忆审理记录。"
    lines = ["最近记忆审理："]
    for decision in decisions:
        lines.append(
            f"- {decision['id']}｜{decision['relation']} → {decision['recommended_action']}｜"
            f"confidence={decision['confidence']:.2f}｜{decision['status']}｜note={decision['note_id']}"
        )
        lines.append(f"  {decision['reason']}")
    return "\n".join(lines)


def format_memory_profile(space_id: str) -> str:
    """函数功能：`format_memory_profile` 负责格式化 memory profile，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    memories = list_memories(space_id, status="active", limit=100)
    if not memories:
        return "还没有足够的长期记忆生成用户画像。"
    # The profile is a current-state projection, not an audit log. Older
    # active V2/V3 rows can coexist with a newer terminal row after a key or
    # wording change, so collapse compatible task identities before rendering.
    task_memories = sorted(
        (memory for memory in memories if memory.memory_type == "task"),
        # SQLite timestamps are second-granular; when two lifecycle writes land
        # in the same second, terminal status must win the current-state view.
        key=lambda memory: (
            memory.updated_at or "",
            1 if memory.task_status == "done" else 0,
            memory.current_version,
            memory.id,
        ),
        reverse=True,
    )
    latest_tasks = []
    for memory in task_memories:
        matched = False
        for existing in latest_tasks:
            if memory.effective_memory_key == existing.effective_memory_key or task_instance_authorized(memory, existing):
                matched = True
                break
        if not matched:
            latest_tasks.append(memory)
    profile_memories = [memory for memory in memories if memory.memory_type != "task"] + latest_tasks
    mismatches = []
    for memory in profile_memories:
        if memory.memory_type != "task" or not memory.task_status:
            continue
        inferred_status = infer_task_status(memory.content)
        if inferred_status and inferred_status != memory.task_status:
            mismatches.append((memory.id, memory.task_status, inferred_status))
    if mismatches:
        LOGGER.warning(
            "profile_task_state_mismatch space_id=%s memories=%s",
            space_id,
            [{"memory_id": memory_id, "stored": stored, "inferred": inferred} for memory_id, stored, inferred in mismatches],
        )
    sections = [
        ("当前任务", [memory for memory in profile_memories if memory.memory_type == "task" and memory.task_status == "todo"]),
        ("偏好与约束", [memory for memory in profile_memories if memory.memory_type == "preference"]),
        ("长期背景", [memory for memory in profile_memories if memory.memory_type == "semantic"]),
        ("近期事件", [memory for memory in profile_memories if memory.memory_type == "episodic"][:5]),
    ]
    lines = ["动态用户画像："]
    for title, items in sections:
        if not items:
            continue
        lines.append(f"\n{title}：")
        for memory in items[:10]:
            task_suffix = f"（{memory.task_status}）" if memory.task_status else ""
            lines.append(f"- {memory.content}{task_suffix}")
    return "\n".join(lines)


def format_memory_stats(space_id: str) -> str:
    """函数功能：`format_memory_stats` 负责格式化 memory stats，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    data = stats(space_id)
    return (
        f"记忆统计：total={data['total']}｜by_type={data['by_type']}｜by_status={data['by_status']}｜"
        f"extraction={data.get('extraction_by_status', {})}｜retryable={data.get('retryable_extraction_count', 0)}｜"
        f"decisions={data.get('decisions_by_relation', {})}"
    )


def format_memory_consolidate(space_id: str, cadence: str) -> str:
    """函数功能：`format_memory_consolidate` 负责格式化 memory consolidate，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        cadence: cadence 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    cadence = cadence.strip().lower()
    if cadence not in {"daily", "weekly", "monthly"}:
        return "用法：/memory consolidate daily｜weekly｜monthly"
    report = run_memory_consolidation_once(cadence, space_ids=[space_id], today=date.today())
    result = (report.get("results") or [{}])[0]
    status = result.get("status")
    if status == "skipped":
        return "本周期已经执行过，未重复运行 consolidation。"
    if status == "failed":
        return f"记忆 consolidation 执行失败：{result.get('error', 'unknown error')}"
    return f"记忆 consolidation 完成：{result}"


def format_trace_latest() -> str:
    """函数功能：`format_trace_latest` 负责格式化 trace latest，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        无。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    trace = latest_trace()
    if trace is None:
        return "还没有 trace。"
    return format_trace(trace)


def format_trace_id(trace_id: str) -> str:
    """函数功能：`format_trace_id` 负责格式化 trace id，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        trace_id: Trace 标识，用于读取或写入审计链路，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    trace = get_trace(trace_id)
    if trace is None:
        return f"没有找到 trace：{trace_id}"
    return format_trace(trace)


def format_trace_memory(memory_id: str) -> str:
    """函数功能：`format_trace_memory` 负责格式化 trace memory，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    traces = find_traces_by_memory(memory_id)
    if not traces:
        return f"没有找到记忆相关 trace：{memory_id}"
    lines = [f"记忆 {memory_id} 相关 trace："]
    for trace in traces[-5:]:
        lines.append(f"- {trace.get('trace_id')}｜{trace.get('trace_type')}｜{trace.get('status')}｜{trace.get('finished_at')}")
    return "\n".join(lines)


def _trace_candidate_lines(trace: dict[str, Any]) -> list[str]:
    """函数功能：`_trace_candidate_lines` 负责追踪 candidate lines，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        trace: trace 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    extracted = [step for step in trace.get("steps", []) if step.get("step") == "candidate_extracted"]
    if not extracted:
        return []

    decisions: dict[str, dict[str, Any]] = {}
    for step in trace.get("steps", []):
        if step.get("step") != "relation_decided":
            continue
        candidate_id = str((step.get("input_summary") or {}).get("candidate_id") or "")
        if candidate_id:
            decisions[candidate_id] = step.get("output_summary") or {}

    lines = [f"候选（{len(extracted)}）："]
    for index, step in enumerate(extracted, start=1):
        summary = step.get("output_summary") or {}
        candidate_id = str(summary.get("candidate_id") or "")
        candidate = get_memory_candidate(candidate_id) if candidate_id else None
        decision = decisions.get(candidate_id, {})
        memory_type = summary.get("memory_type") or (candidate.memory_type if candidate else "unknown")
        confidence = summary.get("confidence") if summary.get("confidence") is not None else (candidate.confidence if candidate else None)
        importance = summary.get("importance") if summary.get("importance") is not None else (candidate.importance if candidate else None)
        content = summary.get("content_preview") or (candidate.content if candidate else "")
        evidence = summary.get("evidence_preview") or (candidate.evidence_span if candidate else "")
        details = [
            f"id={candidate_id or 'unknown'}",
            f"type={memory_type}",
            f"should_store={summary.get('should_store') if summary.get('should_store') is not None else (candidate.should_store if candidate else 'unknown')}",
        ]
        if confidence is not None:
            details.append(f"confidence={float(confidence):.2f}")
        if importance is not None:
            details.append(f"importance={float(importance):.2f}")
        if decision:
            details.append(f"relation={decision.get('relation')}")
            details.append(f"action={decision.get('action')}")
        lines.append(f"  {index}. " + "｜".join(details))
        if content:
            lines[-1] += f"｜内容={safe_text_preview(str(content), limit=90)}"
        elif evidence:
            lines[-1] += f"｜证据={safe_text_preview(str(evidence), limit=90)}"
    return lines


def format_trace(trace: dict[str, Any]) -> str:
    """函数功能：`format_trace` 负责格式化 trace，服务于本文件职责：Memory 公共服务与飞书命令格式化。
    传参：
        trace: trace 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    lines = [
        f"Trace {trace.get('trace_id')}：",
        f"- type：{trace.get('trace_type')}",
        f"- space：{trace.get('space_id')}",
        f"- status：{trace.get('status')}",
        f"- started：{trace.get('started_at')}",
        f"- finished：{trace.get('finished_at')}",
    ]
    steps = trace.get("steps", [])
    lines.append(f"步骤（共 {len(steps)}）：")
    for index, step in enumerate(steps, start=1):
        lines.append(
            f"  {index}. {step.get('step')}｜{step.get('status') or 'unknown'}｜{step.get('duration_ms', 0)}ms"
        )
    lines.extend(_trace_candidate_lines(trace))
    return "\n".join(lines)
