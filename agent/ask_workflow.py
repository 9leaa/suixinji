"""V2 Plan → Execute → Evidence Repair → Answer Ask workflow."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from agent.ask_executor import AskExecutionResult, execute_ask_plan, hydrate_selected_evidence, repair_missing_evidence
from agent.evidence_resolver import resolve_bundle_spans
from agent.fact_resolver import resolve_evidence_facts
from agent.ask_models import AskPlan, AskWorkflowResult, UnitEvidenceBundle
from agent.ask_planner import plan_ask
from agent.ask_plan_validator import safe_fallback_plan
from core import settings
from core.llm_client import complete_json
from memory.consistency import wait_for_memory_barrier
from memory.trace import add_step


ASK_ANSWER_PROMPT = """你是随心记的最终回答器。只输出 JSON：
{"unit_answers":[{"unit_id":"u1","answer":"...","evidence_ids":["..."],"claims":[{"text":"...","evidence_ids":["..."]}]}],"unresolved_units":["u2"]}
- Export a claim only if that factual wording is actually present in the same unit answer.
- Each claim must answer that unit question directly; do not export background or alternative candidate facts.
- Do not output final_answer. The system composes the validated unit answers.


只能依据输入中的 Evidence Bundle 回答：
- 不得编造 Memory、Note、状态、偏好或时间。
- 每个 evidence_ids 必须来自同一 unit 的 evidence_id。
- unit resolution=not_found 时，明确说未找到相关记录。
- unit resolution=conflict 时，明确说证据冲突或无法确认，不得擅自选择一个结论。
- Task 的历史事件不能替代当前 Task 状态。
- 计划/愿望/猜测不能当作已经发生或已经确认的事实。
- 数量、时间顺序或新旧变化只能依据 Bundle 中明确的证据计算；证据不完整时说明缺口。
- facts / fact_summary 是带原文引文的事实清单，不是新的数据库状态；计划/愿望不能作为已发生事实使用。
- 对带有明确名称、引号短语或专有名词的问题，只有证据明确包含该实体时才能直接回答；同类但不同实体只能说明证据不足，不能替代回答。
- 不同时间的同一事实可能是正常更新，不应仅因数值不同就称为冲突；当问题明确问当前/最新结果时，应优先回答时间更晚且实体一致的明确记录。
- 只有同一实体、同一属性、时间无法排序且结论相反时，才把它视为无法裁决的冲突。
- 回答保持简洁、自然。
"""


ASK_ANSWER_CONTRACT_REPAIR_PROMPT = """你是随心记的回答引用修复器。只输出一个 JSON object：
{"unit_answers":[{"unit_id":"u1","answer":"...","evidence_ids":["..."]}]}

只修复输出契约，不增加事实：
- 对每个要回答的 unit，必须同时给出非空 answer 和至少一个 evidence_ids。
- evidence_ids 只能从 allowed_evidence_ids_by_unit 中选择，不能留空、不能编造。
- 没有足够证据的 unit 不要输出。
- 不要输出 claims、final_answer、Markdown 或任何解释。
"""


def _session_context(hook_context: Any) -> dict[str, Any]:
    if hook_context is None or not getattr(hook_context, "session", None):
        return {}
    return {
        key: hook_context.session.get(key)
        for key in ("current_intent", "waiting_for", "pending_operation", "conversation_summary")
        if hook_context.session.get(key) is not None
    }


def _bundle_payload(bundle: UnitEvidenceBundle) -> dict[str, Any]:
    return {
        "unit_id": bundle.unit_id,
        "resolution": bundle.resolution.model_dump(),
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "memory_type": item.memory_type,
                "content": item.content,
                "evidence_span": item.evidence_span,
                "fact_hints": item.fact_hints,
                "event_time": item.event_time,
                "observed_at": item.observed_at,
                "recorded_at": item.recorded_at,
                "role": item.evidence_role,
            }
            for item in bundle.evidence
        ],
        "facts": [fact.model_dump() for fact in bundle.facts],
        "fact_summary": bundle.fact_summary,
    }


def _timeline_topic(content: str) -> str:
    text = str(content or "").strip()
    for marker in (":", "：", "是", "被阻塞", "开始", "完成", "待处理", "取消"):
        if marker in text:
            text = text.split(marker, 1)[0]
            break
    return text.strip(" ，。") or "该事项"


def _status_from_evidence(item: Any) -> str | None:
    # Storage keeps the task lifecycle binary (todo/done); a historical
    # blocker is retained in the version evidence. Prefer that explicit
    # evidence for read-only timeline narration, without mutating state.
    content = str(getattr(item, "content", "") or "")
    if any(marker in content for marker in ("阻塞", "blocked")):
        return "blocked"
    if any(marker in content for marker in ("完成", "做完", "done")):
        return "done"
    status = str(getattr(item, "task_status", "") or "").strip().casefold()
    if status in {"todo", "blocked", "done"}:
        return status
    if any(marker in content for marker in ("开始", "待处理", "todo")):
        return "todo"
    return None


def _timeline_claim_group(unit: Any, bundle: UnitEvidenceBundle) -> dict:
    """Expose a persisted-version sequence without asking an LLM to infer it."""
    items = [item for item in bundle.evidence if item.version_id]
    if len(items) < 2 or len({item.memory_id for item in items}) != 1:
        return {}
    statuses = [_status_from_evidence(item) for item in items]
    topic = _timeline_topic(items[0].content)
    # Task has a controlled state vocabulary. Other memories keep their
    # source-grounded statements verbatim, so the renderer cannot invent a
    # semantic replacement or preference polarity transition.
    if bundle.evidence[0].memory_type == "task" and all(statuses):
        if len(statuses) == 2:
            text = f"{topic}从{statuses[0]}变为{statuses[1]}。"
        else:
            text = f"{topic}先处于{statuses[0]}，随后进入{statuses[1]}，最后转为{statuses[-1]}。"
        member_texts = [f"{topic}是{status}" for status in statuses]
    else:
        text = "；".join(str(item.content).strip("。； ") for item in items) + "。"
        member_texts = [str(item.content) for item in items]
    version_ids = [str(item.version_id) for item in items]
    source_ids = list(dict.fromkeys(source_id for item in items for source_id in item.source_note_ids))
    memory_ids = [str(items[0].memory_id)] if items[0].memory_id else []
    members = [
        {"claim_id": f"{unit.id}:{index}", "text": member_text,
         "memory_ids": memory_ids, "version_ids": [str(item.version_id)],
         "source_ids": list(item.source_note_ids), "support_role": "history"}
        for index, (item, member_text) in enumerate(zip(items, member_texts), start=1)
    ]
    return {
        "group_type": "timeline",
        "summary_claim": {"text": text, "memory_ids": memory_ids, "version_ids": version_ids, "source_ids": source_ids, "support_role": "history"},
        "ordered_member_claim_ids": [item["claim_id"] for item in members],
        "member_claims": members,
        "memory_ids": memory_ids, "version_ids": version_ids, "source_ids": source_ids, "support_role": "history",
    }


def _structured_claims(bundles: list[UnitEvidenceBundle]) -> list[dict]:
    claims: list[dict] = []
    for bundle in bundles:
        for item in bundle.evidence:
            if item.evidence_role == "conflicting":
                continue
            status = _status_from_evidence(item)
            topic = _timeline_topic(item.content)
            text = f"{topic}是{status}" if status and item.memory_type == "task" else item.content
            if text:
                claims.append({
                    "text": text,
                    "memory_ids": [item.memory_id] if item.memory_id else [],
                    "version_ids": [item.version_id] if item.version_id else [],
                    "source_ids": list(item.source_note_ids),
                    "support_role": "history" if item.evidence_role == "historical" else "current",
                })
    return claims


def _fallback_answer(plan: AskPlan, bundles: list[UnitEvidenceBundle]) -> str:
    values: list[str] = []
    by_id = {bundle.unit_id: bundle for bundle in bundles}
    for unit in plan.units:
        bundle = by_id.get(unit.id)
        if bundle is None or bundle.resolution.status == "not_found":
            values.append(f"关于“{unit.question}”，没有找到相关记录。")
        elif bundle.resolution.status == "conflict":
            values.append(f"关于“{unit.question}”，现有记录存在冲突，暂时无法确认。")
        elif unit.intent == "memory_history":
            group = _timeline_claim_group(unit, bundle)
            values.append(str((group.get("summary_claim") or {}).get("text") or bundle.resolution.value or ""))
        elif bundle.resolution.value:
            values.append(bundle.resolution.value)
    return "\n".join(values) or "没有找到足够的相关记录。"


def _selected_records(execution: AskExecutionResult, hydrated_notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_ids = {
        evidence_id
        for bundle in execution.bundles
        for evidence_id in bundle.resolution.selected_evidence_ids
    }
    for records in execution.records_by_unit.values():
        for record in records:
            record_id = str(record.get("id") or record.get("memory_id") or "")
            if record_id and record_id in selected_ids and record_id not in {str(item.get("id") or "") for item in selected}:
                selected.append(record)
    for note in hydrated_notes:
        note_id = str(note.get("id") or "")
        if note_id and note_id not in {str(item.get("id") or "") for item in selected}:
            selected.append(note)
    return selected[:10]


def _claim_identity(text: str) -> str:
    return "".join(char for char in str(text or "").casefold() if char.isalnum())


def _record_matches_selected_evidence(record: dict[str, Any], selected_ids: set[str]) -> bool:
    """Whether a raw retrieval record is the evidence the resolver selected."""
    identifiers = {
        str(record.get(key) or "")
        for key in ("id", "memory_id", "note_id", "version_id")
    }
    return bool((identifiers - {""}) & selected_ids)


def _decision_evidence_from_execution(
    plan: AskPlan,
    execution: AskExecutionResult,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Return only resolver-authorized current/history evidence."""
    units_by_id = {unit.id: unit for unit in plan.units}
    bundle_by_id = {bundle.unit_id: bundle for bundle in execution.bundles}
    current: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    has_resolution_conflict = False
    for unit_id, unit in units_by_id.items():
        bundle = bundle_by_id.get(unit_id)
        if bundle is None:
            continue
        if bundle.resolution.status == "conflict":
            has_resolution_conflict = True
            continue
        if bundle.resolution.status not in {"resolved", "partial"}:
            continue
        selected_ids = {str(value) for value in bundle.resolution.selected_evidence_ids if str(value)}
        if not selected_ids:
            continue
        raw_records = execution.records_by_unit.get(unit_id, [])
        selected_records = [
            record for record in raw_records
            if _record_matches_selected_evidence(record, selected_ids)
        ]
        for record in selected_records:
            is_history = unit.intent == "memory_history" or str(record.get("source_kind") or "") == "memory_version"
            (history if is_history else current).append(record)
        # A conflicting preference is a safety signal rather than answer
        # evidence.  It may block an answer only when it shares the selected
        # record's stable identity key; an unrelated preference cannot do so.
        if unit.intent == "preference_current":
            selected_keys = {
                str(record.get("memory_key") or "")
                for record in selected_records
                if str(record.get("memory_key") or "")
            }
            if selected_keys:
                current.extend(
                    record for record in raw_records
                    if record not in selected_records
                    and str(record.get("memory_key") or "") in selected_keys
                )
    return current, history, has_resolution_conflict


def _claims_from_answer_units(
    accepted: list[tuple[str, list[tuple[str, list[str]]]]],
    evidence_by_unit: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose only facts selected by a rendered Unit answer, never all evidence."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for unit_id, claims in accepted:
        for claim_text, evidence_ids in claims:
            identity = _claim_identity(claim_text)
            if not identity:
                continue
            key = (unit_id, identity)
            record = merged.setdefault(
                key,
                {
                    "text": claim_text.strip(),
                    "memory_ids": [],
                    "version_ids": [],
                    "source_ids": [],
                    "support_role": "current",
                },
            )
            for evidence_id in evidence_ids:
                item = evidence_by_unit.get(unit_id, {}).get(evidence_id)
                if item is None:
                    continue
                if item.memory_id and item.memory_id not in record["memory_ids"]:
                    record["memory_ids"].append(item.memory_id)
                if item.version_id and item.version_id not in record["version_ids"]:
                    record["version_ids"].append(item.version_id)
                for source_id in item.source_note_ids:
                    if source_id not in record["source_ids"]:
                        record["source_ids"].append(source_id)
                if item.evidence_role == "historical":
                    record["support_role"] = "history"
    return list(merged.values())


def _answer_with_claims_from_bundles(
    question: str,
    plan: AskPlan,
    bundles: list[UnitEvidenceBundle],
) -> tuple[str, list[dict[str, Any]]]:
    """Return user-visible Unit answers plus their validated factual claims.

    The model never gets an unbound aggregate answer channel. A claim is
    exported only when it belongs to a rendered Unit answer and cites Evidence
    from that same Unit.
    """
    if plan.answer_mode == "timeline" or any(unit.intent == "memory_history" for unit in plan.units):
        return _fallback_answer(plan, bundles), []
    answerable_bundles = [
        bundle for bundle in bundles
        if bundle.resolution.status in {"resolved", "partial"}
        and bundle.resolution.selected_evidence_ids
    ]
    if not answerable_bundles:
        return _fallback_answer(plan, bundles), []
    payload = {
        "question": question,
        "answer_mode": plan.answer_mode,
        "units": [unit.model_dump() for unit in plan.units],
        "bundles": [_bundle_payload(bundle) for bundle in answerable_bundles],
    }
    evidence_by_unit = {
        bundle.unit_id: {item.evidence_id: item for item in bundle.evidence}
        for bundle in answerable_bundles
    }
    allowed = {unit_id: set(items) for unit_id, items in evidence_by_unit.items()}
    try:
        data = complete_json(
            system_prompt=ASK_ANSWER_PROMPT,
            user_prompt=json.dumps(payload, ensure_ascii=False),
            model_role="fast",
            llm_task="query_synthesis",
            timeout_seconds=getattr(settings, "ASK_ANSWER_TIMEOUT_SECONDS", 20),
        )
        raw_answers = data.get("unit_answers") if isinstance(data, Mapping) else []
        valid_answers: list[str] = []
        rendered_claims: list[tuple[str, list[tuple[str, list[str]]]]] = []
        seen_units: set[str] = set()
        if isinstance(raw_answers, list):
            for item in raw_answers:
                if not isinstance(item, Mapping):
                    continue
                unit_id = str(item.get("unit_id") or "")
                evidence_ids = list(dict.fromkeys(str(value) for value in item.get("evidence_ids") or [] if str(value)))
                if (
                    not unit_id
                    or unit_id in seen_units
                    or unit_id not in allowed
                    or not evidence_ids
                    or not set(evidence_ids).issubset(allowed[unit_id])
                ):
                    continue
                answer = str(item.get("answer") or "").strip()
                if not answer:
                    continue
                seen_units.add(unit_id)
                valid_answers.append(answer)
                unit_claims: list[tuple[str, list[str]]] = []
                raw_claims = item.get("claims")
                if isinstance(raw_claims, list):
                    for raw_claim in raw_claims:
                        if not isinstance(raw_claim, Mapping):
                            continue
                        claim_text = str(raw_claim.get("text") or "").strip()
                        claim_evidence_ids = list(dict.fromkeys(
                            str(value) for value in raw_claim.get("evidence_ids") or [] if str(value)
                        ))
                        if (
                            claim_text
                            and claim_evidence_ids
                            and _claim_identity(claim_text) in _claim_identity(answer)
                            and set(claim_evidence_ids).issubset(set(evidence_ids))
                            and set(claim_evidence_ids).issubset(allowed[unit_id])
                        ):
                            unit_claims.append((claim_text, claim_evidence_ids))
                # Backward-compatible repair for a valid old model payload:
                # its visible Unit answer is the only exportable claim.
                if not unit_claims:
                    unit_claims.append((answer, evidence_ids))
                rendered_claims.append((unit_id, unit_claims))
        if valid_answers:
            return "\n".join(valid_answers), _claims_from_answer_units(rendered_claims, evidence_by_unit)
    except Exception:
        pass
    return _fallback_answer(plan, bundles), []


def _answer_from_bundles(question: str, plan: AskPlan, bundles: list[UnitEvidenceBundle]) -> str:
    """Compatibility wrapper for callers that only need rendered prose."""
    return _answer_with_claims_from_bundles(question, plan, bundles)[0]


def build_shadow_plan(question: str, *, hook_context: Any = None) -> AskPlan:
    return plan_ask(
        question,
        session_context=_session_context(hook_context),
        max_units=getattr(settings, "ASK_MAX_UNITS", 4),
    )


def answer_question_v2(
    space_id: str,
    question: str,
    *,
    hook_context: Any = None,
    trace: dict[str, Any] | None = None,
    access_context: Any = None,
) -> AskWorkflowResult:
    from agent.query_agent import mentions_sensitive_topic
    if mentions_sensitive_topic(question):
        return AskWorkflowResult(
            answer="为保护安全，随心记不会保存或检索密码、密钥、令牌、身份证号、银行卡号等敏感凭据。",
            answer_source="ask_v2_sensitive_query_blocked",
            answer_type="restricted",
            reason_code="sensitive_topic",
            plan=safe_fallback_plan(question),
            bundles=[],
        )
    effective_access_context = access_context
    if effective_access_context is None and hook_context is not None:
        effective_access_context = hook_context.metadata.get("access_context")
    barrier = wait_for_memory_barrier(space_id)
    add_step(
        trace,
        "memory_watermark_barrier",
        status="partial" if barrier.get("status") == "timeout" else "success",
        output_summary=barrier,
        reason="ask_v2_read_after_write",
    )
    planner_started = time.perf_counter()
    plan = build_shadow_plan(question, hook_context=hook_context)
    planner_duration_ms = int((time.perf_counter() - planner_started) * 1000)
    add_step(
        trace,
        "ask_plan_generated",
        duration_ms=planner_duration_ms,
        output_summary={
            "unit_count": len(plan.units),
            "answer_mode": plan.answer_mode,
            "intents": [unit.intent for unit in plan.units],
            "timeout_seconds": getattr(settings, "ASK_PLANNER_TIMEOUT_SECONDS", 12),
        },
        reason="validated_ask_plan",
    )
    add_step(trace, "ask_plan_validated", duration_ms=0, output_summary={"unit_ids": [unit.id for unit in plan.units]})

    # A just-written note may be queryable before memory extraction finishes.
    # Keep the existing read-after-write guarantee, but do not turn that
    # provisional text into a memory-state assertion.
    try:
        from agent.query_agent import _provisional_answer, provisional_search

        provisional = provisional_search(space_id, question, limit=5)
    except Exception:
        provisional = []
    if provisional:
        add_step(
            trace,
            "ask_unit_retrieved",
            status="partial",
            output_summary={"unit_ids": [unit.id for unit in plan.units], "provisional_note_ids": [item.get("id") for item in provisional]},
            reason="ask_v2_provisional_read_after_write",
        )
        return AskWorkflowResult(
            answer=_provisional_answer(provisional),
            answer_source="ask_v2_provisional_read_after_write",
            plan=plan,
            bundles=[],
            selected_records=provisional[:5],
            observations=[{
                "thought": "命中尚未完成抽取的原始笔记，返回 provisional 证据而不推断长期状态。",
                "tool": "provisional_search",
                "args": {"limit": 5},
                "result": provisional[:5],
            }],
        )

    executor_started = time.perf_counter()
    execution = execute_ask_plan(space_id, plan, access_context=effective_access_context)
    repaired_units = repair_missing_evidence(space_id, plan, execution, access_context=effective_access_context)
    executor_duration_ms = int((time.perf_counter() - executor_started) * 1000)
    for bundle in execution.bundles:
        add_step(
            trace,
            "ask_unit_started",
            output_summary={"unit_id": bundle.unit_id},
            reason="deterministic_domain_dispatch",
        )
        add_step(
            trace,
            "ask_unit_retrieved",
            output_summary={
                "unit_id": bundle.unit_id,
                "status": bundle.resolution.status,
                "evidence_ids": bundle.resolution.selected_evidence_ids,
                "conflicting_ids": bundle.resolution.conflicting_evidence_ids,
                "evidence_count": len(bundle.evidence),
                "retrieval_channels": sorted({channel for item in bundle.evidence for channel in item.retrieval_channels}),
                "executor_elapsed_ms": execution.elapsed_ms,
            },
            reason=bundle.resolution.reason_code,
        )
        add_step(
            trace,
            "ask_unit_resolved",
            status="partial" if bundle.resolution.status == "partial" else "success",
            output_summary={"unit_id": bundle.unit_id, "status": bundle.resolution.status},
            reason=bundle.resolution.reason_code,
        )
    for unit_id, error in execution.unit_errors.items():
        add_step(
            trace,
            "ask_unit_retrieved",
            status="partial",
            output_summary={"unit_id": unit_id, "tool": "domain_error"},
            error=error,
            reason="domain_tool_error_isolated",
        )
    if repaired_units:
        add_step(trace, "ask_fallback_executed", output_summary={"unit_ids": repaired_units}, reason="deterministic_evidence_repair")
    hydrated = hydrate_selected_evidence(space_id, plan, execution)
    resolved_evidence_ids = resolve_bundle_spans(plan.units, execution.bundles)
    fact_result = None
    if getattr(settings, "ASK_FACT_RESOLVER_ENABLED", True):
        fact_result = resolve_evidence_facts(question, plan, execution.bundles)
    if hydrated:
        add_step(trace, "ask_evidence_expanded", output_summary={"note_ids": [item.get("id") for item in hydrated]})
    if resolved_evidence_ids:
        add_step(
            trace,
            "ask_evidence_resolved",
            output_summary={"evidence_ids": resolved_evidence_ids},
            reason="bounded_answer_bearing_spans",
        )
    if fact_result is not None:
        add_step(
            trace,
            "ask_facts_resolved",
            status="success" if fact_result.accepted else "partial",
            output_summary={
                "accepted": fact_result.accepted,
                "rejected": fact_result.rejected,
                "rejected_reasons": fact_result.rejected_reasons,
            },
            reason="quote_grounded_atomic_facts",
        )
    answer_started = time.perf_counter()
    from agent.answer_models import AnswerDecision
    from agent.query_agent import _deterministic_route, decide_answer
    current_evidence, history_evidence, has_resolution_conflict = _decision_evidence_from_execution(plan, execution)
    if has_resolution_conflict:
        decision = AnswerDecision("conflict", "unit_resolution_conflict")
    else:
        decision = decide_answer(question, _deterministic_route(question), None,
            current_evidence=current_evidence, history_evidence=history_evidence)
    rendered_claims: list[dict[str, Any]] = []
    answer_contract = {"status": "not_applicable", "attempts": 0, "failure_reason": None}
    if decision.answer_type == "restricted":
        answer = "这类记录受访问权限保护，当前无法提供。"
    elif decision.answer_type == "conflict":
        answer = "相关记录存在冲突，暂时无法确认唯一结论。"
    elif decision.answer_type == "clarification":
        answer = "你指的是哪一个事项？请补充名称或时间。"
    elif decision.answer_type == "no_answer":
        answer = _fallback_answer(plan, execution.bundles)
    elif decision.answer_type == "qualified_history_only":
        history_text = str((history_evidence[0] if history_evidence else {}).get("content") or "\u76f8\u5173\u5386\u53f2\u8bb0\u5f55")
        answer = f"\u53ea\u627e\u5230\u5386\u53f2\u8bb0\u5f55\uff1a{history_text}\uff1b\u65e0\u6cd5\u636e\u6b64\u786e\u8ba4\u5f53\u524d\u72b6\u6001\u3002"
        history_item = next(
            (item for bundle in execution.bundles for item in bundle.evidence if item.evidence_role == "historical"),
            None,
        )
        if history_item and history_text:
            rendered_claims = [{
                "text": history_text,
                "memory_ids": [history_item.memory_id] if history_item.memory_id else [],
                "version_ids": [history_item.version_id] if history_item.version_id else [],
                "source_ids": list(history_item.source_note_ids),
                "support_role": "history",
            }]
    else:
        answer, rendered_claims, answer_contract = _answer_with_claims_from_bundles_detailed(question, plan, execution.bundles)
    # These outcomes do not assert a user-facing factual conclusion.
    if decision.answer_type in {"restricted", "conflict", "clarification", "no_answer"}:
        rendered_claims = []
    answer_duration_ms = int((time.perf_counter() - answer_started) * 1000)
    add_step(
        trace,
        "ask_answer_generated",
        duration_ms=answer_duration_ms,
        output_summary={"answer_len": len(answer), "answer_type": decision.answer_type, "timeout_seconds": getattr(settings, "ASK_ANSWER_TIMEOUT_SECONDS", 20), "answer_contract": answer_contract},
        reason="bundle_constrained_synthesis",
    )
    add_step(
        trace,
        "ask_answer_validated",
        output_summary={"bundle_count": len(execution.bundles), "executor_duration_ms": executor_duration_ms, "timed_out_units": execution.timed_out_units, "answer_contract": answer_contract},
        reason="per_unit_evidence_id_validation",
    )
    add_step(
        trace,
        "ask_finished",
        output_summary={"planner_duration_ms": planner_duration_ms, "executor_duration_ms": executor_duration_ms, "answer_duration_ms": answer_duration_ms, "tool_calls": len(execution.executed_tools)},
        reason="bounded_ask_v2_workflow",
    )
    selected = _selected_records(execution, hydrated)
    claim_groups = [
        group for unit in plan.units
        for bundle in execution.bundles if bundle.unit_id == unit.id
        for group in [_timeline_claim_group(unit, bundle)] if group
    ]
    observations = [
        {
            "thought": "V2 AskPlan 的确定性领域检索结果。",
            "tool": "ask_executor",
            "args": {"unit_count": len(plan.units)},
            "result": [_bundle_payload(bundle) for bundle in execution.bundles],
        }
    ]
    return AskWorkflowResult(
        answer=answer,
        answer_source="ask_v2_plan_execute",
        answer_type=decision.answer_type,
        reason_code="answer_contract_failed" if answer_contract.get("status") == "failed" else decision.reason_code,
        plan=plan,
        bundles=execution.bundles,
        selected_records=selected,
        observations=observations,
        claims=rendered_claims,
        claim_groups=claim_groups,
    )


def _validated_answer_contract_items(
    data: Any,
    allowed: dict[str, set[str]],
) -> tuple[list[Mapping[str, Any]], dict[str, int]]:
    """Validate answer payload IDs before any user-visible text is emitted."""
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    raw_answers = data.get("unit_answers") if isinstance(data, Mapping) else None
    if not isinstance(raw_answers, list):
        reject("unit_answers_not_list")
        return [], rejected
    accepted: list[Mapping[str, Any]] = []
    seen_units: set[str] = set()
    for item in raw_answers:
        if not isinstance(item, Mapping):
            reject("unit_answer_not_object")
            continue
        unit_id = str(item.get("unit_id") or "")
        evidence_ids = list(dict.fromkeys(str(value) for value in item.get("evidence_ids") or [] if str(value)))
        if not unit_id or unit_id not in allowed:
            reject("unit_id_not_allowed")
            continue
        if unit_id in seen_units:
            reject("duplicate_unit_id")
            continue
        if not evidence_ids:
            reject("evidence_ids_empty")
            continue
        if not set(evidence_ids).issubset(allowed[unit_id]):
            reject("evidence_id_not_allowed")
            continue
        if not str(item.get("answer") or "").strip():
            reject("answer_empty")
            continue
        seen_units.add(unit_id)
        accepted.append(item)
    if not accepted and not rejected:
        reject("no_unit_answers")
    return accepted, rejected


def _render_validated_answer_contract(
    items: list[Mapping[str, Any]],
    evidence_by_unit: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Render only validated unit answers and evidence-bound claims."""
    valid_answers: list[str] = []
    rendered_claims: list[tuple[str, list[tuple[str, list[str]]]]] = []
    for item in items:
        unit_id = str(item.get("unit_id") or "")
        evidence_ids = list(dict.fromkeys(str(value) for value in item.get("evidence_ids") or [] if str(value)))
        answer = str(item.get("answer") or "").strip()
        if not unit_id or not answer:
            continue
        valid_answers.append(answer)
        unit_claims: list[tuple[str, list[str]]] = []
        raw_claims = item.get("claims")
        if isinstance(raw_claims, list):
            for raw_claim in raw_claims:
                if not isinstance(raw_claim, Mapping):
                    continue
                claim_text = str(raw_claim.get("text") or "").strip()
                claim_evidence_ids = list(dict.fromkeys(
                    str(value) for value in raw_claim.get("evidence_ids") or [] if str(value)
                ))
                if (
                    claim_text
                    and claim_evidence_ids
                    and _claim_identity(claim_text) in _claim_identity(answer)
                    and set(claim_evidence_ids).issubset(set(evidence_ids))
                    and set(claim_evidence_ids).issubset(set(evidence_by_unit.get(unit_id, {})))
                ):
                    unit_claims.append((claim_text, claim_evidence_ids))
        if not unit_claims:
            unit_claims.append((answer, evidence_ids))
        rendered_claims.append((unit_id, unit_claims))
    return "\n".join(valid_answers), _claims_from_answer_units(rendered_claims, evidence_by_unit)


def _answer_contract_failure_answer(plan: AskPlan, bundles: list[UnitEvidenceBundle]) -> str:
    questions = [
        unit.question for unit in plan.units
        if any(bundle.unit_id == unit.id and bundle.resolution.selected_evidence_ids for bundle in bundles)
    ]
    if questions:
        return "\n".join(f"关于“{question}”，已找到相关证据，但暂时无法生成可验证回答。" for question in questions)
    return "已找到相关证据，但暂时无法生成可验证回答。"


def _answer_with_claims_from_bundles_detailed(
    question: str,
    plan: AskPlan,
    bundles: list[UnitEvidenceBundle],
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Synthesize a citation-bound answer with one bounded contract repair."""
    if plan.answer_mode == "timeline" or any(unit.intent == "memory_history" for unit in plan.units):
        return _fallback_answer(plan, bundles), [], {"status": "not_applicable", "attempts": 0, "failure_reason": None}
    answerable_bundles = [
        bundle for bundle in bundles
        if bundle.resolution.status in {"resolved", "partial"} and bundle.resolution.selected_evidence_ids
    ]
    if not answerable_bundles:
        return _fallback_answer(plan, bundles), [], {"status": "not_applicable", "attempts": 0, "failure_reason": "no_answerable_bundle"}
    payload = {
        "question": question,
        "answer_mode": plan.answer_mode,
        "units": [unit.model_dump() for unit in plan.units],
        "bundles": [_bundle_payload(bundle) for bundle in answerable_bundles],
    }
    evidence_by_unit = {
        bundle.unit_id: {item.evidence_id: item for item in bundle.evidence}
        for bundle in answerable_bundles
    }
    allowed = {unit_id: set(items) for unit_id, items in evidence_by_unit.items()}
    failure_reasons: dict[str, int] = {}
    attempts = 0
    accepted: list[Mapping[str, Any]] = []
    for attempt in range(2):
        attempts += 1
        request_payload = payload
        system_prompt = ASK_ANSWER_PROMPT
        if attempt:
            request_payload = {
                **payload,
                "contract_repair": {
                    "instruction": "Previous output was invalid. Return only the required JSON schema.",
                    "allowed_unit_ids": sorted(allowed),
                    "allowed_evidence_ids_by_unit": {unit_id: sorted(ids) for unit_id, ids in allowed.items()},
                },
            }
            system_prompt = ASK_ANSWER_CONTRACT_REPAIR_PROMPT
        try:
            data = complete_json(
                system_prompt=system_prompt,
                user_prompt=json.dumps(request_payload, ensure_ascii=False),
                model_role="fast",
                llm_task="query_synthesis",
                timeout_seconds=getattr(settings, "ASK_ANSWER_TIMEOUT_SECONDS", 20),
            )
        except Exception as exc:
            reason = f"provider_{type(exc).__name__}"
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
            continue
        accepted, rejected = _validated_answer_contract_items(data, allowed)
        for reason, count in rejected.items():
            failure_reasons[reason] = failure_reasons.get(reason, 0) + count
        if accepted:
            answer, claims = _render_validated_answer_contract(accepted, evidence_by_unit)
            status = "valid" if attempt == 0 else "repaired"
            return answer, claims, {
                "status": status,
                "attempts": attempts,
                "failure_reason": None,
                "failure_reasons": failure_reasons,
                "answered_units": [str(item.get("unit_id") or "") for item in accepted],
            }
    failure_reason = next(iter(failure_reasons), "no_valid_unit_answer")
    return _answer_contract_failure_answer(plan, bundles), [], {
        "status": "failed",
        "attempts": attempts,
        "failure_reason": failure_reason,
        "failure_reasons": failure_reasons,
        "answered_units": [],
    }
