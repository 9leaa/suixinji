"""Evidence-bounded atomic fact extraction for Ask V2.

The extractor may describe what a selected evidence span says, but it is never
allowed to mutate task state, preference polarity, or memory conflict status.
Every accepted fact keeps a verbatim quote from its source evidence.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from agent.ask_models import AskPlan, EvidenceFact, UnitEvidenceBundle
from core import settings
from core.llm_client import complete_json


FACT_PROMPT = """You extract atomic facts from supplied evidence, not answers.
Return JSON only: {"facts":[...]}. Each fact must have:
{"unit_id":"u1","evidence_id":"...","quote":"verbatim source quote","claim":"short factual statement","modality":"asserted|planned|uncertain","subject":"...|null","predicate":"...|null","value":"...|null","event_time":"...|null","item_key":"...|null","quantity":number|null}

Rules:
- Use only the supplied evidence spans. quote must be copied verbatim from one span.
- Distinguish an achieved/observed assertion from a plan, desire, possibility, or uncertainty.
- Do not infer a current task status, preference polarity, conflict winner, missing item, total, or answer.
- quantity is allowed only when its number is explicit in quote. item_key is an item identity, not a guess.
- Omit a fact when its source quote is not direct enough.
"""


@dataclass
class FactResolutionResult:
    accepted: int = 0
    rejected: int = 0
    rejected_reasons: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected += 1
        self.rejected_reasons[reason] = self.rejected_reasons.get(reason, 0) + 1


def _normalise(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _quote_is_grounded(quote: str, evidence_text: str) -> bool:
    compact = _normalise(quote)
    return len(compact) >= 3 and compact in _normalise(evidence_text)


def _quantity_is_grounded(quantity: float | None, quote: str) -> bool:
    if quantity is None:
        return True
    rendered = str(int(quantity)) if float(quantity).is_integer() else str(quantity)
    return bool(re.search(rf"(?<!\d){re.escape(rendered)}(?!\d)", quote))


def _fact_from_raw(raw: Any, evidence_by_id: dict[str, str], valid_units: set[str], result: FactResolutionResult) -> EvidenceFact | None:
    if not isinstance(raw, dict):
        result.reject("not_object")
        return None
    try:
        fact = EvidenceFact.model_validate(raw)
    except Exception:
        result.reject("schema_invalid")
        return None
    if fact.unit_id not in valid_units:
        result.reject("unknown_unit")
        return None
    evidence_text = evidence_by_id.get(fact.evidence_id)
    if not evidence_text:
        result.reject("unknown_evidence")
        return None
    if not _quote_is_grounded(fact.quote, evidence_text):
        result.reject("quote_not_grounded")
        return None
    if not _quantity_is_grounded(fact.quantity, fact.quote):
        result.reject("quantity_not_grounded")
        return None
    return fact


def _bundle_summary(facts: Iterable[EvidenceFact]) -> str | None:
    """A bounded, non-decisive fact inventory for the answer model."""
    parts: list[str] = []
    for fact in facts:
        modality = {"asserted": "明确陈述", "planned": "计划/愿望", "uncertain": "不确定"}[fact.modality]
        detail = fact.claim
        if fact.event_time:
            detail = f"{detail}（时间：{fact.event_time}）"
        if detail not in parts:
            parts.append(f"[{modality}] {detail}")
    return "；".join(parts)[:1000] or None


def resolve_evidence_facts(question: str, plan: AskPlan, bundles: list[UnitEvidenceBundle]) -> FactResolutionResult:
    """Attach quote-grounded atomic facts to evidence bundles in one LLM call.

    A failure is intentionally non-fatal: the existing bounded evidence answer
    path remains available, while trace records the absence of fact resolution.
    """
    eligible = [bundle for bundle in bundles if bundle.evidence and bundle.resolution.status != "not_found"]
    result = FactResolutionResult()
    if not eligible:
        return result
    units = {unit.id for unit in plan.units}
    payload = {
        "question": question,
        "units": [
            {
                "unit_id": bundle.unit_id,
                "evidence_mode": next((unit.evidence_mode for unit in plan.units if unit.id == bundle.unit_id), "aggregate"),
                "evidence": [
                    {"evidence_id": item.evidence_id, "span": item.evidence_span or item.content}
                    for item in bundle.evidence
                    if item.evidence_span or item.content
                ],
            }
            for bundle in eligible
        ],
    }
    evidence_by_id = {
        item.evidence_id: item.evidence_span or item.content
        for bundle in eligible
        for item in bundle.evidence
        if item.evidence_span or item.content
    }
    if not evidence_by_id:
        return result
    try:
        raw = complete_json(
            system_prompt=FACT_PROMPT,
            user_prompt=json.dumps(payload, ensure_ascii=False),
            model_role="fast",
            llm_task="query_synthesis",
            timeout_seconds=getattr(settings, "ASK_ANSWER_TIMEOUT_SECONDS", 20),
        )
    except Exception:
        result.reject("llm_unavailable")
        return result
    raw_facts = raw.get("facts") if isinstance(raw, dict) else None
    if not isinstance(raw_facts, list):
        result.reject("facts_missing")
        return result
    by_unit: dict[str, list[EvidenceFact]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()
    for raw_fact in raw_facts[:32]:
        fact = _fact_from_raw(raw_fact, evidence_by_id, units, result)
        if fact is None:
            continue
        identity = (fact.unit_id, fact.evidence_id, _normalise(fact.quote))
        if identity in seen:
            continue
        seen.add(identity)
        by_unit[fact.unit_id].append(fact)
        result.accepted += 1
    for bundle in eligible:
        bundle.facts = by_unit.get(bundle.unit_id, [])[:12]
        bundle.fact_summary = _bundle_summary(bundle.facts)
    return result
