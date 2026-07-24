"""Optional strong-model advisory for high-risk Memory relations."""

from __future__ import annotations

import json
from typing import Any

from core import settings
from core.llm_client import complete_json
from memory.models import MemoryCandidate, MemoryDecision, MemoryRecord

ADVISORY_PROMPT = """
你是随心记长期记忆的审阅助手。
你只能给出关系建议，不能执行删除、覆盖、merge 或 supersede。
必须输出 JSON object：
{"recommended_relation":"conflict|supersede|merge|same|new|uncertain","confidence":0.0,"reason":"...","evidence_ids":[]}
"""

HIGH_RISK_ACTIONS = {"supersede", "conflict", "pending_review", "merge"}


def maybe_memory_relation_advisory(
    candidate: MemoryCandidate,
    memories: list[MemoryRecord],
    decision: MemoryDecision,
) -> dict[str, Any] | None:
    if not settings.STRONG_ESCALATION_ENABLED or decision.recommended_action not in HIGH_RISK_ACTIONS:
        return None
    payload = {
        "candidate": {
            "id": candidate.candidate_id,
            "type": candidate.memory_type,
            "content": candidate.content,
            "memory_key": candidate.effective_memory_key,
            "confidence": candidate.confidence,
        },
        "local_decision": {
            "relation": decision.relation,
            "action": decision.recommended_action,
            "confidence": decision.confidence,
            "reason": decision.reason,
        },
        "memories": [
            {
                "id": memory.id,
                "type": memory.memory_type,
                "content": memory.content,
                "memory_key": memory.effective_memory_key,
                "status": memory.status,
            }
            for memory in memories[:6]
        ],
    }
    data = complete_json(
        system_prompt=ADVISORY_PROMPT,
        user_prompt=json.dumps(payload, ensure_ascii=False),
        llm_task="memory_conflict_advisory",
    )
    return {
        "recommended_relation": str(data.get("recommended_relation") or "uncertain"),
        "confidence": float(data.get("confidence") or 0.0),
        "reason": str(data.get("reason") or "")[:500],
        "evidence_ids": [str(item) for item in data.get("evidence_ids") or []][:10],
    }
