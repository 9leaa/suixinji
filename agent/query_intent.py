"""Structured, fast-model query intent classification for Memory V3."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from core.llm_client import complete_json
from memory.prompts import QUERY_INTENT_PROMPT


class QueryIntent(BaseModel):
    intent: Literal[
        "task_status",
        "preference",
        "current_fact",
        "note_history",
        "recent_notes",
        "relationship",
        "summary",
        "general_search",
    ]
    entity: str | None = None
    attribute: str | None = None
    topic: str | None = None
    time_scope: Literal["current", "history", "recent", "all"] | None = None
    confidence: float = Field(ge=0, le=1)
    complexity: Literal["simple", "complex", "uncertain"] = "uncertain"
    strategies: list[Literal["none", "rewrite", "decomposition", "step_back"]] = Field(default_factory=list)
    rewritten_queries: list[str] = Field(default_factory=list)
    sub_questions: list[dict[str, Any]] = Field(default_factory=list)
    step_back_query: str | None = None

    @field_validator("entity", "attribute", "topic", mode="before")
    @classmethod
    def _clean_optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text[:160] or None


def classify_query_intent(question: str) -> QueryIntent | None:
    """Return a validated intent, or None so the legacy router can take over."""
    question = str(question or "").strip()
    if not question:
        return None
    try:
        data = complete_json(
            system_prompt=QUERY_INTENT_PROMPT,
            user_prompt=json.dumps({"question": question}, ensure_ascii=False),
            model_role="fast",
            llm_task="query_intent",
        )
        return QueryIntent.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        return None
    except Exception:
        # Model/network failures deliberately fall back to the established
        # deterministic and ReAct routes.
        return None


def _retrieval_query(question: str, intent: QueryIntent) -> str:
    """Add model slots and explicit identifiers as separate retrieval terms.

    A Chinese question such as “RAG 学到哪里了” is often one compact token in
    PostgreSQL lexical search.  Supplying the extracted topic and its ASCII
    identifiers separately lets the same retrieval path match task content
    without enumerating question phrasings.
    """
    terms = [str(question or "").strip()]
    terms.extend(str(value).strip() for value in (intent.topic, intent.attribute, intent.entity) if value)
    terms.extend(re.findall(r"[A-Za-z][A-Za-z0-9+#._-]*", question))
    return " ".join(dict.fromkeys(term for term in terms if term))[:600]


def is_task_inventory_question(question: str) -> bool:
    """Whether the user asks to list tasks rather than inspect one task.

    A task inventory has no specific task subject.  Memory lookup remains the
    first source of truth, but a note-type fallback is more dependable than
    semantic similarity when no durable task memory has been written yet.
    """
    compact = re.sub(r"\s+", "", str(question or "")).casefold()
    has_task_noun = any(marker in compact for marker in ("任务", "待办", "todo"))
    has_listing_cue = any(marker in compact for marker in ("哪些", "有什么", "有啥", "清单", "列表", "列出"))
    return has_task_noun and has_listing_cue


def _query_plan_payload(intent: QueryIntent) -> dict[str, object]:
    return {
        "complexity": intent.complexity,
        "strategies": list(intent.strategies),
        "rewritten_queries": list(intent.rewritten_queries),
        "sub_questions": list(intent.sub_questions),
        "step_back_query": intent.step_back_query,
    }


def route_for_intent(intent: QueryIntent, question: str, *, memory_min_score: float, note_min_score: float, top_k: int) -> dict[str, object] | None:
    """Map a memory-oriented intent to a bounded tool route."""
    memory_type = {
        "task_status": "task",
        "preference": "preference",
        "current_fact": "semantic",
    }.get(intent.intent)
    if memory_type is None:
        # A valid non-memory intent must still suppress the legacy shortcut
        # that sends every short sentence directly to note semantic search.
        # The query agent interprets this sentinel as Memory prefetch + ReAct.
        return {
            "action": "memory_prefetch",
            "args": {},
            "synthesize": True,
            "reason": f"query_intent:{intent.intent}",
            "intent": intent.intent,
            "query_plan": _query_plan_payload(intent),
        }
    retrieval_query = _retrieval_query(question, intent)
    fallback: dict[str, object] = {
        "action": "memory_note_fallback",
        "args": {"query": retrieval_query, "limit": top_k, "min_score": note_min_score},
    }
    if memory_type == "task" and not any((intent.topic, intent.attribute)) and is_task_inventory_question(question):
        fallback = {
            "action": "filter_notes",
            "args": {"type": "任务", "limit": 30},
        }
    return {
        "action": "memory_search",
        "args": {
            "query": retrieval_query,
            "memory_type": memory_type,
            "limit": 8 if memory_type == "task" else 5,
            "min_score": memory_min_score,
        },
        "fallback": fallback,
        "synthesize": True,
        "reason": f"query_intent:{intent.intent}",
        "intent": intent.intent,
        "query_plan": _query_plan_payload(intent),
    }
