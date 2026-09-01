"""Deterministic, query-aware Note fusion and reranking.

This module only reorders already retrieved Note candidates. It never writes
memory, infers a fact, or reads outside the provided candidate set.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
import re
from typing import Any, Mapping, Sequence


_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "do", "did", "does", "i", "me", "my",
    "you", "your", "we", "our", "to", "of", "in", "on", "at", "for", "with", "and", "or",
    "what", "which", "who", "when", "where", "how", "can", "could", "would", "please", "about",
    "that", "this", "it", "all", "last", "time", "previously", "before", "after", "again",
})
_ASSISTANT_CUES = re.compile(
    r"\b(?:you\s+(?:recommended|suggested|told\s+me|mentioned|explained|said)|"
    r"your\s+(?:recommendation|advice|answer)|we\s+(?:discussed|talked))\b",
    re.I,
)
_USER_CUES = re.compile(r"\b(?:i|me|my|mine)\b", re.I)
_MULTI_CUES = re.compile(
    r"\b(?:total|both|between|all|each|and|compare|versus|vs\.?|how\s+many|how\s+much|"
    r"more\s+.+\s+than|less\s+.+\s+than|compared\s+to|previously)\b|"
    r"(?:分别|一共|总共|比较|之间|以及|和)",
    re.I,
)
_CURRENT_CUES = re.compile(
    r"\b(?:now|currently|current|latest|most\s+recent|today)\b|(?:现在|当前|目前|最新|最近)",
    re.I,
)


@dataclass(frozen=True)
class QueryConstraints:
    terms: tuple[str, ...]
    quoted_phrases: tuple[str, ...]
    role_mode: str | None
    needs_multi_evidence: bool
    prefers_recent_evidence: bool


def _tokens(text: str) -> list[str]:
    """Extract content-bearing English and Chinese terms without a model."""
    values: list[str] = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*|[\u3400-\u9fff]{2,}", str(text or "").casefold()):
        if token in _STOPWORDS or len(token) < 2 or token in values:
            continue
        values.append(token)
        if re.fullmatch(r"[\u3400-\u9fff]{4,}", token):
            values.extend(
                piece
                for piece in (token[index:index + 2] for index in range(len(token) - 1))
                if piece not in values
            )
    return values[:24]


def parse_query_constraints(query: str) -> QueryConstraints:
    value = " ".join(str(query or "").split())
    assistant = bool(_ASSISTANT_CUES.search(value))
    return QueryConstraints(
        terms=tuple(dict.fromkeys(_stem(token) for token in _tokens(value))),
        quoted_phrases=tuple(
            phrase.casefold().strip()
            for phrase in re.findall(r"['\"]([^'\"]{2,120})['\"]", value)
            if phrase.strip()
        )[:3],
        # Assistant wording overrides first-person wording in “I remember you told me”.
        role_mode="assistant" if assistant else ("user" if _USER_CUES.search(value) else None),
        needs_multi_evidence=bool(_MULTI_CUES.search(value)),
        prefers_recent_evidence=bool(_CURRENT_CUES.search(value)),
    )


def _record_id(record: Mapping[str, Any]) -> str:
    return str(record.get("id") or record.get("note_id") or "")


def _record_text(record: Mapping[str, Any]) -> str:
    return " ".join(
        str(record.get(key) or "")
        for key in ("title", "summary", "text", "full_text", "snippet")
    ).casefold()


def _stem(token: str) -> str:
    if re.fullmatch(r"[a-z][a-z'_-]*", token):
        for suffix in ("ing", "ed", "es", "s"):
            if len(token) > len(suffix) + 2 and token.endswith(suffix):
                return token[:-len(suffix)]
    return token


def _record_terms(record: Mapping[str, Any]) -> set[str]:
    return {_stem(token) for token in _tokens(_record_text(record))}


def _record_roles(record: Mapping[str, Any], roles_by_id: Mapping[str, Sequence[str]] | None) -> set[str]:
    values: list[Any] = []
    record_id = _record_id(record)
    if roles_by_id and record_id:
        values.extend(roles_by_id.get(record_id) or [])
    for key in ("speaker_roles", "source_roles", "roles", "source_role", "role"):
        value = record.get(key)
        values.extend(value if isinstance(value, (list, tuple, set)) else [value])
    return {str(value).casefold() for value in values if str(value or "").strip()}


def _record_timestamp(record: Mapping[str, Any]) -> str:
    """Return an ISO-sortable source time when the candidate exposes one."""
    return max(
        str(record.get(key) or "")
        for key in ("ts", "updated_at", "created_at", "source_created_at", "observed_at")
    )


def fuse_note_variants(
    groups: Sequence[Sequence[Mapping[str, Any]]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Fuse original and rewrite lanes, with a lower weight for expansions."""
    rows: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = defaultdict(float)
    lanes: dict[str, list[str]] = defaultdict(list)
    for lane_index, group in enumerate(groups):
        # First lane is exact user wording; others expand recall but must not
        # outvote it merely because a shortened rewrite is more generic.
        lane_weight = 1.0 if lane_index == 0 else max(0.58, 0.82 - 0.08 * (lane_index - 1))
        for rank, raw in enumerate(group, start=1):
            record = dict(raw)
            record_id = _record_id(record)
            if not record_id:
                continue
            rows.setdefault(record_id, record)
            scores[record_id] += lane_weight / (60.0 + rank)
            lanes[record_id].append(f"query_variant_{lane_index + 1}")
    ordered_ids = sorted(
        rows,
        key=lambda record_id: (scores[record_id], str(rows[record_id].get("ts") or ""), record_id),
        reverse=True,
    )
    output = []
    for record_id in ordered_ids[: max(1, min(int(limit), 50))]:
        item = dict(rows[record_id])
        item["v2_base_score"] = round(scores[record_id], 8)
        item["v2_rewrite_lanes"] = lanes[record_id]
        output.append(item)
    return output


def _normalize(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    low, high = min(values.values()), max(values.values())
    if math.isclose(low, high):
        return {key: 1.0 for key in values}
    return {key: (value - low) / (high - low) for key, value in values.items()}


def _max_jaccard(terms: set[str], selected: Sequence[set[str]]) -> float:
    if not terms or not selected:
        return 0.0
    return max(len(terms & other) / max(1, len(terms | other)) for other in selected)


def rerank_note_records(
    query: str,
    records: Sequence[Mapping[str, Any]],
    *,
    roles_by_id: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """Use query constraints to resolve RRF near-ties without a model.

    The score combines the existing Hybrid/RRF rank with term coverage, rare
    entity coverage, quoted phrase support and explicit source-role support.
    For aggregate/comparison questions only ranks after the first are mildly
    diversified, so a single-document lookup keeps its best answer at rank 1.
    """
    if not records:
        return []
    constraints = parse_query_constraints(query)
    materialized = [dict(record) for record in records]
    doc_terms = {_record_id(record): _record_terms(record) for record in materialized}
    frequencies = {
        term: sum(1 for values in doc_terms.values() if term in values)
        for term in constraints.terms
    }
    # Use a smooth rank prior rather than min/max normalising tiny RRF
    # differences: the latter turns rank 1 vs. rank 2 into 1.0 vs. 0.0.
    base = {
        _record_id(record): 1.0 / (1.0 + 0.22 * index)
        for index, record in enumerate(materialized)
        if _record_id(record)
    }
    query_terms = set(constraints.terms)
    total_idf = sum(1.0 / max(1, frequencies.get(term, 0)) for term in query_terms) or 1.0
    ranked: list[tuple[float, dict[str, Any], set[str]]] = []
    for record in materialized:
        record_id = _record_id(record)
        terms = doc_terms.get(record_id, set())
        overlap = query_terms & terms
        coverage = len(overlap) / max(1, len(query_terms))
        rare_coverage = sum(1.0 / max(1, frequencies[term]) for term in overlap) / total_idf
        text = _record_text(record)
        phrase_bonus = 0.04 if any(phrase in text for phrase in constraints.quoted_phrases) else 0.0
        role_bonus = 0.10 if constraints.role_mode in _record_roles(record, roles_by_id) else 0.0
        score = 0.48 * base.get(record_id, 0.0) + 0.32 * coverage + 0.13 * rare_coverage + phrase_bonus + role_bonus
        item = dict(record)
        item["v2_constraint_coverage"] = round(coverage, 6)
        item["v2_rare_entity_coverage"] = round(rare_coverage, 6)
        item["v2_role_bonus"] = role_bonus
        item["v2_constraint_score"] = round(score, 6)
        ranked.append((score, item, terms))
    ranked.sort(key=lambda row: (row[0], str(row[1].get("ts") or ""), _record_id(row[1])), reverse=True)
    if not constraints.needs_multi_evidence or len(ranked) < 3:
        return [item for _score, item, _terms in ranked]

    remaining = ranked[:]
    selected = [remaining.pop(0)]
    selected_terms = [selected[0][2]]
    while remaining:
        index, choice = max(
            enumerate(remaining),
            key=lambda pair: (
                pair[1][0] - 0.12 * _max_jaccard(pair[1][2], selected_terms),
                str(pair[1][1].get("ts") or ""),
            ),
        )
        remaining.pop(index)
        selected.append(choice)
        selected_terms.append(choice[2])
    return [item for _score, item, _terms in selected]


def select_answer_evidence(
    query: str,
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Choose a compact, answer-facing Note evidence set from retrieved rows.

    This is deterministic and read-only. It rewards query identity coverage,
    including rare terms and quoted phrases; prefers newer evidence only for
    explicitly current questions; and reduces duplicate context for direct
    questions. It never changes the retrieval candidate pool or creates facts.
    """
    if not records:
        return []
    constraints = parse_query_constraints(query)
    materialized = [dict(record) for record in records]
    doc_terms = {_record_id(record): _record_terms(record) for record in materialized}
    query_terms = set(constraints.terms)
    frequencies = {
        term: sum(1 for terms in doc_terms.values() if term in terms)
        for term in query_terms
    }
    total_idf = sum(1.0 / max(1, frequencies[term]) for term in query_terms) or 1.0
    timestamps = {_record_id(record): _record_timestamp(record) for record in materialized}
    distinct_times = sorted({value for value in timestamps.values() if value})
    time_rank = {value: index / max(1, len(distinct_times) - 1) for index, value in enumerate(distinct_times)}
    scored: list[tuple[float, dict[str, Any], set[str]]] = []
    for index, record in enumerate(materialized):
        record_id = _record_id(record)
        terms = doc_terms.get(record_id, set())
        overlap = query_terms & terms
        coverage = len(overlap) / max(1, len(query_terms))
        rare_coverage = sum(1.0 / max(1, frequencies[term]) for term in overlap) / total_idf
        text = _record_text(record)
        phrase_coverage = sum(1 for phrase in constraints.quoted_phrases if phrase in text) / max(1, len(constraints.quoted_phrases))
        rank_prior = 1.0 / (1.0 + 0.20 * index)
        freshness = time_rank.get(timestamps.get(record_id, ""), 0.0) if constraints.prefers_recent_evidence else 0.0
        score = 0.48 * rank_prior + 0.28 * coverage + 0.17 * rare_coverage + 0.05 * phrase_coverage + 0.10 * freshness
        item = dict(record)
        item["answer_evidence_score"] = round(score, 6)
        item["answer_evidence_coverage"] = round(coverage, 6)
        item["answer_evidence_rare_coverage"] = round(rare_coverage, 6)
        scored.append((score, item, terms))
    scored.sort(key=lambda row: (row[0], _record_timestamp(row[1]), _record_id(row[1])), reverse=True)
    target = max(1, min(int(limit), 5 if constraints.needs_multi_evidence else 3))
    selected: list[tuple[float, dict[str, Any], set[str]]] = []
    while scored and len(selected) < target:
        if not selected:
            choice_index = 0
        else:
            selected_terms = [terms for _score, _item, terms in selected]
            duplicate_penalty = 0.10 if constraints.needs_multi_evidence else 0.18
            choice_index, _choice = max(
                enumerate(scored),
                key=lambda pair: (
                    pair[1][0] - duplicate_penalty * _max_jaccard(pair[1][2], selected_terms),
                    _record_timestamp(pair[1][1]),
                ),
            )
        score, item, terms = scored.pop(choice_index)
        item["answer_evidence_rank"] = len(selected) + 1
        item["answer_evidence_selected"] = True
        selected.append((score, item, terms))
    return [item for _score, item, _terms in selected]
