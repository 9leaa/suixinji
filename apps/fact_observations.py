"""Read-only diagnostic projection. Never reconstruct old model inputs from live memory."""
import json
from core.lifecycle import safe
from memory import fact_memory as fm


def protect_history(space, records):
    # A new diagnostic surface must not expose forgotten evidence via old snapshots.
    tombstones = fm.items(space, "tombstone")
    source_ids = {t["note_id"] for t in tombstones}
    facts = {f["id"] for f in fm.items(space, "fact") if f.get("note_id") in source_ids}
    cards = {c["id"] for c in fm.items(space, "card") if set(c.get("source_fact_ids", [])) & facts}
    tokens = {json.dumps(ident, ensure_ascii=False) for ident in source_ids | facts | cards}
    tokens.update(t["evidence_span"] for t in tombstones if t.get("evidence_span"))
    if not tokens:
        return safe(records)
    result = []
    for row in records:
        raw = json.dumps(row, ensure_ascii=False)
        if any(token in raw for token in tokens):
            result.append({k: row[k] for k in ("id", "trace_id", "created_at", "started_at", "status", "memory_type") if k in row}
                          | {"withheld": "该历史记录涉及已遗忘的来源，诊断正文已隐藏。"})
        else:
            result.append(row)
    return safe(result)


def read_observations(space):
    from sqlalchemy import select
    from infrastructure.database import session_scope
    from infrastructure.schema import MemoryTrace
    with session_scope() as session:
        query_rows = session.scalars(select(MemoryTrace).where(
            MemoryTrace.space_id == space, MemoryTrace.trace_type == "memory_query"
        ).order_by(MemoryTrace.started_at.desc()).limit(60)).all()
        queries = [dict(row.payload_json) for row in query_rows
                   if any(step.get("step") in {"query_plan", "query_routed", "answer_returned"}
                          for step in row.payload_json.get("steps", []))][:20]
    latest = lambda kind, clock: sorted(fm.items(space, kind), key=lambda r: r.get(clock, ""), reverse=True)[:20]
    return {"extraction": protect_history(space, latest("extraction_context", "created_at")),
            "consolidation": protect_history(space, latest("run", "started_at")),
            "query": protect_history(space, queries),
            "limit": 20, "space_id": space}
