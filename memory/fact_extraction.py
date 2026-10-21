"""Natural-language fact extraction contract, independent of task/preference slots."""
from memory.models import MemoryCandidate
from memory import fact_memory as fm

INSTRUCTIONS = """
仅对 semantic/episodic 使用以下补充规则；task/preference 维持前述协议：
- fact_context 是同一空间的历史证据，只用于核对主体、时间与指代，不重复抽取历史事实。
- content 用完整自然语言保留当前消息的主体、否定、假设、更正、时间限定和实质细节；
  无法确定主体时保留原指代、reference_status=unresolved，绝不默认改成“用户”。
- 省略主语、位置描述、同名者的角色/所属项目等都可用于消解，但必须有匹配证据，
  不能因为只召回一个候选就猜。允许上下文充分的自动解析，不要求用户每句重复全名。
- 多个对象都符合当前消息时，最近提到、出现顺序、相似主题不能单独证明指代。
  没有角色、所属项目、位置、明确回复等区分依据，必须 unresolved，content 保留原指代。
- 利用 fact_context 消解时用 context_fact_ids 引用其 id；本条 evidence_span 必须仍是当前原文连续片段。
- 假设与未发生的事不是已经发生的事件；“仍然/没有变/刚才是假设”等更正信息本身值得保存。
- 明确陈述的当前否定状态也是事实，不能因为与待办有关就丢弃；只排除未来行动意图，
  不将“尚未发生”改写成“已经发生”，也不把事实再写成新的任务。
- 时间指代也需要核对前文：有明确更早证据时在正文保留对应日期并引用 context_fact_ids；
  召回顺序不是对话顺序，按 mentioned_at/ingested_at 核对，不用后来提到的日期倒推早先的“当天”。
- semantic/episodic 不需要强填 entity/attribute/new_value，content 与 evidence_span 才是事实依据。
- valid_from/valid_until 可保留不精确的年份或日期文字，不能将年份解释成 Unix 秒。
"""


def context(space, note_id, text):
    candidates = fm.search_memory(space, text, limit=12)
    # Context continuity complements topic search; neither alone proves identity.
    recent = sorted(fm.active_facts(space), key=lambda f: f["ingested_at"], reverse=True)[:8]
    rows = {r["id"]: r for r in candidates + recent if r.get("note_id") != note_id}
    result = sorted((fm.model_evidence(space, r) for r in rows.values()),
                    key=lambda r: str(r.get("mentioned_at") or r.get("created_at") or r.get("ingested_at", "")))
    fm.store.put(space, "fm_extraction_context", note_id,
                 {"id": note_id, "status": "prepared", "query": text, "result_ids": [r["id"] for r in result], "created_at": fm.now_iso(),
                  "retrieved": candidates, "recent": [fm.model_evidence(space, r) for r in recent],
                  "model_context": result, "parameters": {"search_limit": 12, "recent_limit": 8},
                  "selection": "相关召回 + 最近事实，排除当前笔记、按 ID 去重后按消息时间排序"})
    return result


def candidate(note_id, raw, row, history):
    span = str(row.get("evidence_span") or "")
    if not span or span not in raw:
        raise ValueError("fact extraction requires a verbatim current-message span")
    content = str(row.get("content") or span).strip()
    references = row.get("context_fact_ids") or []
    allowed = {r["id"] for r in history}
    if not set(references) <= allowed:
        raise ValueError("fact context reference is outside retrieved evidence")
    state = row.get("reference_status", "not_applicable")
    scope = {"reference_status": state,
             "context_fact_ids": references,
             "time_evidence": {k: row[k] for k in ("valid_from", "valid_until") if row.get(k)}}
    for field in ("antecedent_note_id", "antecedent_evidence_span", "resolution_confidence"):
        if row.get(field) is not None:
            scope[field] = row[field]
    return MemoryCandidate(
        note_id=note_id, memory_type=row["memory_type"], content=content,
        evidence_span=span, entities=row.get("entities") or [],
        importance=float(row.get("importance", .6)), confidence=float(row.get("confidence", .8)),
        should_store=row.get("should_store", True), scope=scope,
        extraction_reason=str(row.get("extraction_reason") or "fact_evidence_extraction"),
        extractor_type="llm", extractor_version="fact-evidence-v1",
    )


def context_finished(space, note_id, *, error=None):
    row = fm.get(space, "extraction_context", note_id)
    if row is not None:
        row.update(status="failed" if error else "completed", finished_at=fm.now_iso())
        if error:
            row["error"] = type(error).__name__
        fm.store.put(space, "fm_extraction_context", note_id, row)
