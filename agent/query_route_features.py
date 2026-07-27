"""Deterministic structural features for query routing.

The feature extractor deliberately does not try to understand the answer.  It
only identifies query shape (clauses, entities, operations and ambiguity) so a
simple query can stay on the fast path and an uncertain query can be escalated
to the structured QueryIntent/Planner model.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_CLAUSE_SPLIT_RE = re.compile(r"(?:并且|同时|另外|还有|以及|然后|最后|此外|；|;|\n|[?？])")
_NEGATION_RE = re.compile(r"(?:不要|无需|不需要|不必|不用|无须|别|don't|do not|without)\s*$", re.IGNORECASE)
_NEGATED_OPERATION_PREFIX_RE = re.compile(r"(?:不要|无需|不需要|不必|不用|无须|别|don't|do not|without)[^，,。；;？?]{0,8}$", re.IGNORECASE)
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]*")

_COMPARISON_MARKERS = ("比较", "对比", "区别", "差异", "compare", "contrast", "difference")
_CAUSAL_MARKERS = ("为什么", "原因", "因为什么", "为何", "why", "because", "cause", "reason")
_TREND_MARKERS = ("变化", "趋势", "演变", "前后", "历史", "演进", "change", "trend", "evolution", "over time")
_RELATION_MARKERS = ("关联", "关系", "之间", "影响", "相关性", "relationship", "related", "impact")
_SUMMARY_MARKERS = ("总结", "归纳", "概括", "汇总", "总结一下", "summary", "summarize", "overall")
_ANAPHORA_MARKERS = ("那个", "这个", "那件事", "这件事", "它", "之前说的", "上次", "前面提到", "the one", "that thing", "it", "previous")
_AMBIGUOUS_ROUTE_MARKERS = ("怎么样", "怎么了", "有进展吗", "做到哪", "换得", "进展如何", "how is", "what happened")
_SINGLE_SCOPE_MARKERS = ("只查询", "只查找", "唯一当前", "单独查询", "only", "just look up", "exactly one")
_MULTI_STEP_RE = re.compile(r"(?:先|首先).*(?:再|然后|接着).*(?:最后|再给出|并给出)|step\s*by\s*step|first.*then.*finally", re.IGNORECASE)


@dataclass(frozen=True)
class QueryRouteFeatures:
    normalized_query: str
    language: str
    clauses: tuple[str, ...]
    question_clause_count: int
    entity_candidates: tuple[str, ...]
    intent_candidates: tuple[str, ...]
    time_scopes: tuple[str, ...]
    has_comparison: bool
    has_causal_request: bool
    has_trend_request: bool
    has_relationship_request: bool
    has_summary_request: bool
    has_multi_step_request: bool
    has_anaphora: bool
    negated_operations: tuple[str, ...]
    explicit_identifiers: tuple[str, ...]


@dataclass(frozen=True)
class StructuralRouteDecision:
    complexity: str
    confidence: float
    suggested_strategies: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def is_confident_simple(self) -> bool:
        return self.complexity == "simple" and self.confidence >= 0.90


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _active_markers(text: str, markers: tuple[str, ...]) -> tuple[str, ...]:
    active: list[str] = []
    for marker in markers:
        for match in re.finditer(re.escape(marker), text, flags=re.IGNORECASE):
            prefix = text[max(0, match.start() - 8):match.start()]
            wider_prefix = text[max(0, match.start() - 16):match.start()]
            if not _NEGATION_RE.search(prefix) and not _NEGATED_OPERATION_PREFIX_RE.search(wider_prefix):
                active.append(marker)
                break
    return tuple(dict.fromkeys(active))


def _split_clauses(text: str) -> tuple[str, ...]:
    parts = [_clean(part.strip(" ，,。！？?!")) for part in _CLAUSE_SPLIT_RE.split(text)]
    return tuple(dict.fromkeys(part for part in parts if len(part) >= 2))


def _compare_entities(text: str) -> list[str]:
    patterns = (
        r"(?:比较|对比)\s*([^，,。；;？?]{2,40}?)\s*(?:和|与|及|vs\.?|VS\.?|versus)\s*([^，,。；;？?的]{2,40})",
        r"compare\s+(.+?)\s+(?:and|vs\.?|versus)\s+(.+?)(?:\s+and\s+|\s+which\b|$)",
        r"(?:分别|各自)\s*(?:找出|查询|说明)?\s*([^，,。；;？?]{2,40}?)\s*(?:和|与|及)\s*([^，,。；;？?]{2,40}?)(?:的记录|的状态|的结论|之间|的关系|$)",
    )
    result: list[str] = []
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            result.extend(_clean(group).strip(" ，,。！？?!") for group in match.groups())
    return list(dict.fromkeys(item for item in result if len(item) >= 2))[:4]


def _contains_anaphora(text: str) -> bool:
    lowered = text.casefold()
    for marker in _ANAPHORA_MARKERS:
        if marker == "它" and "其它" in text:
            if re.search(r"(?<!其)它", text):
                return True
            continue
        if marker.isascii():
            if re.search(rf"\b{re.escape(marker.casefold())}\b", lowered):
                return True
        elif marker in text:
            return True
    return False


def extract_route_features(question: str) -> QueryRouteFeatures:
    text = _clean(question)
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_ascii = bool(re.search(r"[A-Za-z]", text))
    language = "mixed" if has_cjk and has_ascii else "zh" if has_cjk else "en" if has_ascii else "unknown"

    clauses = _split_clauses(text)
    active_comparison = _active_markers(text, _COMPARISON_MARKERS)
    active_causal = _active_markers(text, _CAUSAL_MARKERS)
    active_trend = _active_markers(text, _TREND_MARKERS)
    active_relation = _active_markers(text, _RELATION_MARKERS)
    active_summary = _active_markers(text, _SUMMARY_MARKERS)
    explicit_identifiers = tuple(dict.fromkeys(_ASCII_TOKEN_RE.findall(text)))
    entity_candidates = _compare_entities(text)
    if not entity_candidates:
        entity_candidates = list(explicit_identifiers[:4])

    negated_operations: list[str] = []
    for marker_group in (_COMPARISON_MARKERS, _CAUSAL_MARKERS, _SUMMARY_MARKERS):
        for marker in marker_group:
            if re.search(rf"(?:不要|无需|不需要|不必|不用|无须|别|don't|do not|without)\s*(?:再\s*)?{re.escape(marker)}", text, re.IGNORECASE):
                negated_operations.append(marker)

    intents: list[str] = []
    if any(marker in text for marker in ("任务", "待办", "进度", "完成", "做到哪", "task", "todo", "status")):
        intents.append("task_status")
    if any(marker in text for marker in ("喜欢", "偏好", "习惯", "讨厌", "preference")):
        intents.append("preference")
    if any(marker in text for marker in ("笔记", "记录", "历史", "note", "history")):
        intents.append("note_history")
    if active_relation:
        intents.append("relationship")
    if active_summary:
        intents.append("summary")
    if not intents:
        intents.append("general_search")

    time_scopes: list[str] = []
    if any(marker in text for marker in ("现在", "当前", "目前", "today", "current")):
        time_scopes.append("current")
    if any(marker in text for marker in ("最近", "近期", "这几天", "recent", "lately")):
        time_scopes.append("recent")
    if any(marker in text for marker in ("之前", "历史", "上个月", "过去", "before", "history")):
        time_scopes.append("history")
    if not time_scopes:
        time_scopes.append("all")

    return QueryRouteFeatures(
        normalized_query=text,
        language=language,
        clauses=clauses,
        question_clause_count=max(1, len(clauses)) if text else 0,
        entity_candidates=tuple(entity_candidates),
        intent_candidates=tuple(dict.fromkeys(intents)),
        time_scopes=tuple(dict.fromkeys(time_scopes)),
        has_comparison=bool(active_comparison),
        has_causal_request=bool(active_causal),
        has_trend_request=bool(active_trend),
        has_relationship_request=bool(active_relation),
        has_summary_request=bool(active_summary),
        has_multi_step_request=bool(_MULTI_STEP_RE.search(text)) or len(clauses) >= 2,
        has_anaphora=_contains_anaphora(text),
        negated_operations=tuple(dict.fromkeys(negated_operations)),
        explicit_identifiers=explicit_identifiers,
    )


def classify_structural_route(features: QueryRouteFeatures) -> StructuralRouteDecision:
    if not features.normalized_query:
        return StructuralRouteDecision("simple", 1.0, ("none",), ("empty_query",))

    reasons: list[str] = []
    strategies: list[str] = []
    explicit_step_back = "step-back" in features.normalized_query.casefold() or "step back" in features.normalized_query.casefold()
    if explicit_step_back:
        strategies.append("step_back")
        reasons.append("explicit_step_back")
    explicit_complex = any(
        (
            features.has_comparison,
            features.has_causal_request,
            features.has_trend_request,
            features.has_relationship_request,
            features.has_summary_request,
            features.has_multi_step_request,
            explicit_step_back,
        )
    )
    if features.has_comparison:
        strategies.append("decomposition")
        reasons.append("comparison_or_difference")
    if features.has_multi_step_request:
        strategies.append("decomposition")
        reasons.append("multiple_question_clauses")
    if features.has_causal_request or features.has_trend_request:
        strategies.append("step_back")
        reasons.append("causal_or_temporal_analysis")
    if features.has_relationship_request or features.has_summary_request:
        strategies.append("decomposition")
        if features.has_relationship_request:
            reasons.append("relationship_analysis")
        if features.has_summary_request:
            reasons.append("evidence_aggregation")
    if features.has_anaphora:
        strategies.append("rewrite")
        reasons.append("unresolved_reference")

    strategies = list(dict.fromkeys(strategies))
    if explicit_complex:
        return StructuralRouteDecision("complex", 0.96 if strategies else 0.88, tuple(strategies or ["rewrite"]), tuple(reasons))

    if features.negated_operations and not features.has_anaphora:
        return StructuralRouteDecision("simple", 0.93, ("none",), ("negated_complex_operations",))

    # Length is a reason to ask for a second opinion, never a reason by itself
    # to launch expensive decomposition.
    if any(marker.casefold() in features.normalized_query.casefold() for marker in _SINGLE_SCOPE_MARKERS):
        return StructuralRouteDecision("simple", 0.93, ("none",), ("explicit_single_scope",))
    if len(features.normalized_query) > 50:
        return StructuralRouteDecision("uncertain", 0.58, ("rewrite",), ("long_or_mixed_query",))
    if features.has_anaphora:
        return StructuralRouteDecision("uncertain", 0.62, ("rewrite",), tuple(reasons or ("unresolved_reference",)))

    if any(marker.casefold() in features.normalized_query.casefold() for marker in _AMBIGUOUS_ROUTE_MARKERS):
        return StructuralRouteDecision("uncertain", 0.64, ("rewrite",), ("ambiguous_status_wording",))

    return StructuralRouteDecision("simple", 0.94, ("none",), ("single_clause_single_intent",))


def structural_route(question: str) -> tuple[QueryRouteFeatures, StructuralRouteDecision]:
    features = extract_route_features(question)
    return features, classify_structural_route(features)


def should_call_query_intent_llm(question: str) -> bool:
    """Return whether intent/planning needs a model second opinion."""
    _, decision = structural_route(question)
    return decision.complexity != "simple" or decision.confidence < 0.90
