"""Public orchestration and command formatting for long-term memory."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from datetime import date
from typing import Any

from agent.hooks import AgentRunContext, get_default_hook_manager
from core.settings import MEMORY_EXTRACTOR_MODE, MEMORY_QUERY_MIN_SCORE
from memory.consolidator import consolidate_candidate
from memory.candidate_validator import contains_sensitive_data, validate_candidates
from memory.extractor import extract_candidates
from memory.models import candidate_id_for
from memory.repository import (
    approve_pending_memory,
    correct_memory,
    edit_pending_memory,
    get_extraction_state,
    get_memory_candidate_status,
    get_memory,
    list_memories,
    list_memory_decisions,
    list_memory_relations,
    mark_extraction_completed,
    mark_extraction_empty,
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
from memory.trace import add_step, find_traces_by_memory, finish_trace, get_trace, latest_trace, start_trace


def _note_value(note: Any, key: str, default: Any = None) -> Any:
    if is_dataclass(note):
        return asdict(note).get(key, default)
    if isinstance(note, dict):
        return note.get(key, default)
    return getattr(note, key, default)


def _process_note_memory_impl(note: Any, classification: dict[str, Any] | None = None) -> dict[str, Any]:
    note_id = str(_note_value(note, "id", ""))
    space_id = str(_note_value(note, "space_id", ""))
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
        state = mark_extraction_processing(note_id, space_id)
        add_step(
            trace,
            "extraction_state_processing",
            output_summary={"note_id": note_id, "attempt_count": state.attempt_count},
        )
        extracted_candidates = extract_candidates(note_id, text, classification=classification)
        enriched_candidates = [
            replace(
                candidate,
                note_id=note_id,
                space_id=space_id,
                candidate_id=candidate_id_for(note_id, candidate.memory_type, candidate.content),
            )
            for candidate in extracted_candidates
        ]
        for candidate in enriched_candidates:
            save_memory_candidate(candidate, space_id=space_id, status="extracted")
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
) -> list[dict[str, Any]]:
    trace = start_trace("memory_query", space_id, query_len=len(query))
    add_step(trace, "query_received", input_summary={"query_len": len(query), "memory_type": memory_type, "min_score": min_score})
    results = [
        {**memory.to_dict(), "score": score}
        for memory, score in search_memories(space_id, query, memory_type=memory_type, min_score=min_score, limit=limit)
        if not contains_sensitive_data(memory.content)
    ]
    add_step(
        trace,
        "memory_search",
        output_summary={"result_count": len(results), "memory_ids": [item["id"] for item in results]},
    )
    finish_trace(trace)
    return results


def _format_memory(memory: dict[str, Any]) -> str:
    source_count = len(memory.get("sources") or [])
    score = memory.get("score")
    score_text = f"｜score={score}" if score is not None else ""
    return (
        f"- {memory.get('id')}｜{memory.get('memory_type')}｜{memory.get('status')}{score_text}\n"
        f"  {memory.get('content')}\n"
        f"  sources={source_count}｜updated={memory.get('updated_at')}"
    )


def format_memory_list(space_id: str, *, status: str = "active", limit: int = 20) -> str:
    memories = [
        memory.to_dict()
        for memory in list_memories(space_id, status=status, limit=limit)
        if not contains_sensitive_data(memory.content)
    ]
    if not memories:
        return "没有找到长期记忆。"
    return "长期记忆：\n" + "\n".join(_format_memory(memory) for memory in memories)


def format_memory_show(memory_id: str) -> str:
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
    results = memory_search(space_id, query)
    if not results:
        return "没有找到匹配的长期记忆。"
    return "记忆检索结果：\n" + "\n".join(_format_memory(item) for item in results)


def format_memory_forget(memory_id: str) -> str:
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


def format_memory_correct(memory_id: str, content: str) -> str:
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
    memory = correct_memory(memory_id, content)
    if memory is None:
        add_step(trace, "memory_control_failed", status="failed", output_summary={"memory_id": memory_id, "action": "correct"})
        finish_trace(trace, status="failed")
        return f"没有找到记忆：{memory_id}"
    add_step(trace, "memory_corrected", output_summary={"memory_id": memory_id, "version": memory.current_version})
    finish_trace(trace)
    return f"已修正记忆：{memory_id}\n{memory.content}"


def format_memory_conflicts(space_id: str) -> str:
    memories = [memory.to_dict() for memory in list_memories(space_id, status="conflicted", limit=50)]
    if not memories:
        return "当前没有 conflicted 记忆。"
    return "冲突记忆：\n" + "\n".join(_format_memory(memory) for memory in memories)


def format_memory_pending(space_id: str) -> str:
    memories = [memory.to_dict() for memory in list_memories(space_id, status="pending_review", limit=50)]
    if not memories:
        return "当前没有 pending_review 记忆。"
    return "待审记忆：\n" + "\n".join(_format_memory(memory) for memory in memories)


def format_memory_reject(memory_id: str, reason: str = "user_rejected_pending_memory") -> str:
    memory = reject_pending_memory(memory_id, reason=reason)
    if memory is None:
        return f"没有找到待审记忆：{memory_id}"
    return f"已拒绝待审记忆：{memory_id}"


def format_memory_edit(memory_id: str, content: str) -> str:
    if contains_sensitive_data(content):
        return "修正内容包含敏感凭据，未写入长期记忆。"
    memory = edit_pending_memory(memory_id, content)
    if memory is None:
        return f"没有找到待审记忆：{memory_id}"
    return f"已编辑并批准记忆：{memory.id}\n{memory.content}"


def format_memory_resolve(memory_id: str, resolution: str, content: str | None = None) -> str:
    memory = resolve_memory_conflict(memory_id, resolution=resolution, content=content)
    if memory is None:
        return f"没有找到冲突记忆：{memory_id}"
    return f"已解决冲突：{memory.id}\n{memory.content}"


def format_memory_approve(memory_id: str) -> str:
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
    memories = list_memories(space_id, status="active", limit=100)
    if not memories:
        return "还没有足够的长期记忆生成用户画像。"
    sections = [
        ("当前任务", [memory for memory in memories if memory.memory_type == "task" and memory.task_status not in {"done", "cancelled"}]),
        ("偏好与约束", [memory for memory in memories if memory.memory_type == "preference"]),
        ("长期背景", [memory for memory in memories if memory.memory_type == "semantic"]),
        ("近期事件", [memory for memory in memories if memory.memory_type == "episodic"][:5]),
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
    data = stats(space_id)
    return (
        f"记忆统计：total={data['total']}｜by_type={data['by_type']}｜by_status={data['by_status']}｜"
        f"extraction={data.get('extraction_by_status', {})}｜retryable={data.get('retryable_extraction_count', 0)}｜"
        f"decisions={data.get('decisions_by_relation', {})}"
    )


def format_memory_consolidate(space_id: str, cadence: str) -> str:
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
    trace = latest_trace()
    if trace is None:
        return "还没有 trace。"
    return format_trace(trace)


def format_trace_id(trace_id: str) -> str:
    trace = get_trace(trace_id)
    if trace is None:
        return f"没有找到 trace：{trace_id}"
    return format_trace(trace)


def format_trace_memory(memory_id: str) -> str:
    traces = find_traces_by_memory(memory_id)
    if not traces:
        return f"没有找到记忆相关 trace：{memory_id}"
    lines = [f"记忆 {memory_id} 相关 trace："]
    for trace in traces[-5:]:
        lines.append(f"- {trace.get('trace_id')}｜{trace.get('trace_type')}｜{trace.get('status')}｜{trace.get('finished_at')}")
    return "\n".join(lines)


def format_trace(trace: dict[str, Any]) -> str:
    lines = [
        f"Trace {trace.get('trace_id')}：",
        f"- type：{trace.get('trace_type')}",
        f"- space：{trace.get('space_id')}",
        f"- status：{trace.get('status')}",
        f"- started：{trace.get('started_at')}",
        f"- finished：{trace.get('finished_at')}",
    ]
    for step in trace.get("steps", [])[-12:]:
        lines.append(f"  - {step.get('step')}｜{step.get('status')}｜{step.get('reason') or ''}")
    return "\n".join(lines)
