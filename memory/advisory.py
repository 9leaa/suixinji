"""文件作用：人工审查建议。

项目关系：本文件依赖 `core`、`core.llm_client`、`memory.models`；被 `memory.consolidator`。
"""



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

IDENTITY_PROMPT = """
你是随心记的记忆身份比较器。你只能比较当前候选与给出的真实 Memory，不能执行写库。
Task 输出 identity_relation=same_instance|same_family|different|uncertain；
Preference 输出 identity_relation=same_assertion|same_family|different|uncertain。
必须输出 JSON object：
{"identity_relation":"uncertain","target_memory_id":null,"confidence":0.0,"reason_code":"...","supporting_fields":[],"conflicting_fields":[]}
不得根据状态词本身判断任务身份；型号、外部编号、scope、polarity 冲突必须列入 conflicting_fields。
"""

HIGH_RISK_ACTIONS = {"supersede", "conflict", "pending_review", "merge"}


def maybe_memory_identity_adjudication(
    candidate: MemoryCandidate,
    memories: list[MemoryRecord],
) -> dict[str, Any] | None:
    """Run semantic identity comparison only after broad local family recall."""
    if not settings.STRONG_ESCALATION_ENABLED or candidate.memory_type not in {"task", "preference"} or not memories:
        return None
    payload = {
        "candidate": {
            "type": candidate.memory_type,
            "content": candidate.content,
            "task_status": candidate.task_status,
            "polarity": candidate.polarity,
            "scope": candidate.scope,
        },
        "memories": [
            {
                "id": memory.id,
                "type": memory.memory_type,
                "content": memory.content,
                "task_status": memory.task_status,
                "polarity": memory.polarity,
                "scope": memory.scope,
            }
            for memory in memories[:8]
        ],
    }
    data = complete_json(
        system_prompt=IDENTITY_PROMPT,
        user_prompt=json.dumps(payload, ensure_ascii=False),
        llm_task="memory_identity_adjudication",
    )
    allowed = {
        "task": {"same_instance", "same_family", "different", "uncertain"},
        "preference": {"same_assertion", "same_family", "different", "uncertain"},
    }[candidate.memory_type]
    relation = str(data.get("identity_relation") or "uncertain")
    target_id = str(data.get("target_memory_id") or "") or None
    valid_ids = {memory.id for memory in memories[:8]}
    if relation not in allowed or (target_id is not None and target_id not in valid_ids):
        relation, target_id = "uncertain", None
    return {
        "identity_relation": relation,
        "target_memory_id": target_id,
        "confidence": min(1.0, max(0.0, float(data.get("confidence") or 0.0))),
        "reason_code": str(data.get("reason_code") or "")[:160],
        "supporting_fields": [str(item) for item in data.get("supporting_fields") or []][:12],
        "conflicting_fields": [str(item) for item in data.get("conflicting_fields") or []][:12],
    }


def maybe_memory_relation_advisory(
    candidate: MemoryCandidate,
    memories: list[MemoryRecord],
    decision: MemoryDecision,
) -> dict[str, Any] | None:
    """函数功能：`maybe_memory_relation_advisory` 负责处理 maybe memory relation advisory，服务于本文件职责：人工审查建议。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memories: memories 参数，由调用方传入，类型为 `list[MemoryRecord]`。
        decision: decision 参数，由调用方传入，类型为 `MemoryDecision`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
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
