"""Public service API and command formatting for Memory V2."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from memory.consolidator import consolidate_candidate
from memory.extractor import extract_candidates
from memory.repository import correct_memory, get_memory, list_memories, purge_memory, search_memories, soft_delete_memory, stats
from memory.scheduler import run_memory_consolidation
from memory.trace import add_step, find_traces_by_memory, finish_trace, get_trace, latest_trace, start_trace


def _note_value(note: Any, key: str, default: Any = None) -> Any:
    if is_dataclass(note):
        return asdict(note).get(key, default)
    if isinstance(note, dict):
        return note.get(key, default)
    return getattr(note, key, default)


def process_note_memory(note: Any, classification: dict[str, Any] | None = None) -> dict[str, Any]:
    note_id = str(_note_value(note, "id", ""))
    space_id = str(_note_value(note, "space_id", ""))
    text = str(_note_value(note, "text", "") or "")
    trace = start_trace("memory_write", space_id, note_id=note_id)
    add_step(trace, "note_saved", output_summary={"note_id": note_id, "text_len": len(text)})
    add_step(trace, "memory_extraction_started", input_summary={"note_id": note_id, "text_len": len(text)})

    try:
        candidates = extract_candidates(note_id, text, classification=classification)
        for candidate in candidates:
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
                reason=candidate.reason,
            )

        results = [consolidate_candidate(space_id, note_id, candidate, trace=trace) for candidate in candidates]
        add_step(trace, "vector_written", output_summary={"note_id": note_id, "memory_count": len(results)}, reason="note_vector_written_before_memory")
        finish_trace(trace)
        return {"note_id": note_id, "space_id": space_id, "candidates": len(candidates), "results": results, "trace_id": trace["trace_id"]}
    except Exception as exc:
        add_step(trace, "memory_write_failed", status="failed", error=str(exc))
        finish_trace(trace, status="failed")
        raise


def memory_search(space_id: str, query: str, *, memory_type: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
    trace = start_trace("memory_query", space_id, query_len=len(query))
    add_step(trace, "query_received", input_summary={"query_len": len(query), "memory_type": memory_type})
    results = [
        {**memory.to_dict(), "score": score}
        for memory, score in search_memories(space_id, query, memory_type=memory_type, limit=limit)
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
    memories = [memory.to_dict() for memory in list_memories(space_id, status=status, limit=limit)]
    if not memories:
        return "没有找到长期记忆。"
    return "长期记忆：\n" + "\n".join(_format_memory(memory) for memory in memories)


def format_memory_show(memory_id: str) -> str:
    memory = get_memory(memory_id)
    if memory is None:
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
    return "\n".join(lines)


def format_memory_search(space_id: str, query: str) -> str:
    results = memory_search(space_id, query)
    if not results:
        return "没有找到匹配的长期记忆。"
    return "记忆检索结果：\n" + "\n".join(_format_memory(item) for item in results)


def format_memory_forget(memory_id: str) -> str:
    memory = soft_delete_memory(memory_id)
    if memory is None:
        return f"没有找到记忆：{memory_id}"
    return f"已软删除记忆：{memory_id}"


def format_memory_purge(memory_id: str) -> str:
    if not purge_memory(memory_id):
        return f"没有找到记忆：{memory_id}"
    return f"已彻底删除记忆：{memory_id}"


def format_memory_correct(memory_id: str, content: str) -> str:
    memory = correct_memory(memory_id, content)
    if memory is None:
        return f"没有找到记忆：{memory_id}"
    return f"已修正记忆：{memory_id}\n{memory.content}"


def format_memory_conflicts(space_id: str) -> str:
    memories = [memory.to_dict() for memory in list_memories(space_id, status="conflicted", limit=50)]
    if not memories:
        return "当前没有 conflicted 记忆。"
    return "冲突记忆：\n" + "\n".join(_format_memory(memory) for memory in memories)


def format_memory_stats(space_id: str) -> str:
    data = stats(space_id)
    return f"记忆统计：total={data['total']}｜by_type={data['by_type']}｜by_status={data['by_status']}"


def format_memory_consolidate(space_id: str, cadence: str) -> str:
    try:
        result = run_memory_consolidation(space_id, cadence)
    except ValueError:
        return "用法：/memory consolidate daily｜weekly｜monthly"
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
