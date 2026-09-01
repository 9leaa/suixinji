"""Bounded, deterministic evidence preparation for the V2 Ask answerer.

This module does not decide a user's task state, preference polarity, or
semantic conflicts.  It only turns already-authorized retrieved records into
answer-bearing evidence spans and explicit surface facts (numbers/dates).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from agent.ask_models import QueryUnit, UnitEvidenceBundle
from core import settings


_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*|[\u4e00-\u9fff]{2,}")
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+|\n+")
_NUMBER_RE = re.compile(
    r"(?<!\d)\d{1,2}:\d{2}(?!\d)|(?<!\d)\d+(?:\.\d+)?(?!\d)|[零一二三四五六七八九十百千万两]+(?:个|件|天|周|月|年|次|小时|分钟)?"
)
_DATE_RE = re.compile(
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b|\d{1,2}月\d{1,2}[日号]?"
)


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _WORD_RE.findall(text or "")
        if len(token) >= 2 and token.lower() not in {"what", "when", "where", "with", "have", "this", "that", "about", "我的", "什么", "这个"}
    }


def _sentences(text: str) -> list[str]:
    return [" ".join(piece.split()) for piece in _SENTENCE_RE.split(text or "") if piece and piece.strip()]


_TURN_RE = re.compile(r"(?im)^(user|assistant)\s*:\s*")
_ORDINAL_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b|第\s*(\d{1,2})\s*(?:个|项|条)?")
_LIST_ITEM_RE = re.compile(r"(?<!\d)(\d{1,2})[.)]\s+")


def _transcript_turns(text: str) -> list[tuple[str, str]]:
    """Split normalized Note transcripts into role-labelled conversational turns."""
    matches = list(_TURN_RE.finditer(text or ""))
    if not matches:
        return []
    turns: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = " ".join(text[match.end():end].split())
        if content:
            turns.append((match.group(1).lower(), content))
    return turns


def _ordinal_from_question(question: str) -> int | None:
    match = _ORDINAL_RE.search(question or "")
    if not match:
        return None
    value = next((group for group in match.groups() if group), None)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _numbered_item(text: str, ordinal: int) -> str | None:
    markers = list(_LIST_ITEM_RE.finditer(text or ""))
    for index, marker in enumerate(markers):
        if int(marker.group(1)) != ordinal:
            continue
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        return " ".join(text[marker.start():end].split())
    return None


def _turn_excerpt(text: str, *, limit: int, ordinal: int | None = None) -> str:
    compact = " ".join((text or "").split())
    if ordinal is not None:
        item = _numbered_item(compact, ordinal)
        if item:
            return item[:limit]
    return compact[:limit]


def _conversation_span(unit: QueryUnit, text: str, *, max_chars: int) -> str:
    """Keep a query-matched turn together with the answer turn that follows it.

    Conversation Notes often store the relevant wording in a user turn while
    the answer-bearing entity is in the following assistant turn. Sentence
    scoring alone separates the two and can truncate a numbered answer list.
    """
    turns = _transcript_turns(text)
    if len(turns) < 2:
        return ""
    terms = _tokens(f"{unit.question} {' '.join(unit.source_spans)}")
    numeric_or_temporal = _question_requests_numeric_or_temporal_fact(unit.question, unit)
    scored = [
        (_score_sentence(content, terms=terms, source_spans=unit.source_spans, numeric_or_temporal=numeric_or_temporal), index)
        for index, (_role, content) in enumerate(turns)
    ]
    best_score, anchor = max(scored, key=lambda row: (row[0], 1 if turns[row[1]][0] == "user" else 0, -row[1]))
    if best_score <= 0:
        return ""

    role, content = turns[anchor]
    ordinal = _ordinal_from_question(unit.question)
    if role == "user" and anchor + 1 < len(turns) and turns[anchor + 1][0] == "assistant":
        user_excerpt = _turn_excerpt(content, limit=min(360, max_chars // 2))
        assistant_budget = max(160, max_chars - len(user_excerpt) - 20)
        assistant_excerpt = _turn_excerpt(turns[anchor + 1][1], limit=assistant_budget, ordinal=ordinal)
        return f"user: {user_excerpt}\nassistant: {assistant_excerpt}"[:max_chars]
    if role == "assistant":
        return f"assistant: {_turn_excerpt(content, limit=max_chars, ordinal=ordinal)}"[:max_chars]
    return f"{role}: {_turn_excerpt(content, limit=max_chars, ordinal=ordinal)}"[:max_chars]


def _question_requests_numeric_or_temporal_fact(question: str, unit: QueryUnit) -> bool:
    compact = (question or "").lower()
    markers = (
        "how many", "how much", "how long", "which first", "when", "count", "total",
        "多少", "几个", "几件", "几天", "几次", "多久", "什么时候", "先后", "总共", "总计",
    )
    return unit.evidence_mode in {"aggregate", "timeline"} or any(marker in compact for marker in markers)


def _score_sentence(sentence: str, *, terms: set[str], source_spans: Iterable[str], numeric_or_temporal: bool) -> int:
    lowered = sentence.lower()
    score = sum(4 for term in terms if term in lowered)
    score += sum(10 for span in source_spans if len(span.strip()) >= 2 and span.lower() in lowered)
    if numeric_or_temporal and _NUMBER_RE.search(sentence):
        score += 3
    if numeric_or_temporal and _DATE_RE.search(sentence):
        score += 3
    return score


def select_evidence_span(unit: QueryUnit, text: str) -> str:
    """Choose a small set of relevant sentence windows from a retrieved Note.

    Full text is read only after the Note has already been selected by the
    executor.  The returned span is bounded before it reaches the answer LLM.
    """
    max_chars = max(300, int(getattr(settings, "ASK_EVIDENCE_SPAN_CHARS", 900)))
    conversation_span = _conversation_span(unit, text, max_chars=max_chars)
    if conversation_span:
        return conversation_span
    sentences = _sentences(text)
    if not sentences:
        return ""
    terms = _tokens(f"{unit.question} {' '.join(unit.source_spans)}")
    numeric_or_temporal = _question_requests_numeric_or_temporal_fact(unit.question, unit)
    ranked = sorted(
        enumerate(sentences),
        key=lambda pair: _score_sentence(
            pair[1], terms=terms, source_spans=unit.source_spans, numeric_or_temporal=numeric_or_temporal,
        ),
        reverse=True,
    )
    selected_indexes: list[int] = []
    # Keep at most two distinct windows.  Aggregate/timeline questions often
    # need more than one fact, while direct questions remain compact.
    wanted = 2 if numeric_or_temporal else 1
    for index, sentence in ranked:
        if _score_sentence(sentence, terms=terms, source_spans=unit.source_spans, numeric_or_temporal=numeric_or_temporal) <= 0:
            break
        if index not in selected_indexes:
            selected_indexes.append(index)
        if len(selected_indexes) >= wanted:
            break
    if not selected_indexes:
        selected_indexes = [0]

    max_chars = max(300, int(getattr(settings, "ASK_EVIDENCE_SPAN_CHARS", 900)))
    windows: list[str] = []
    for index in sorted(selected_indexes):
        # Include one neighbour so a bare number/date retains its subject.
        start, end = max(0, index - 1), min(len(sentences), index + 2)
        window = " ".join(sentences[start:end])
        if window and window not in windows:
            windows.append(window)
    return "\n…\n".join(windows)[:max_chars]


def extract_fact_hints(text: str, *, limit: int = 8) -> list[str]:
    """Expose quoted numbers/dates as evidence anchors, not inferred answers."""
    facts: list[str] = []
    for match in list(_DATE_RE.finditer(text or "")) + list(_NUMBER_RE.finditer(text or "")):
        value = match.group(0).strip()
        if value and value not in facts:
            facts.append(value)
        if len(facts) >= limit:
            break
    return facts


def resolve_bundle_spans(plan_units: Iterable[QueryUnit], bundles: Iterable[UnitEvidenceBundle]) -> list[str]:
    """Populate bounded spans and fact hints for all selected evidence items."""
    units = {unit.id: unit for unit in plan_units}
    touched: list[str] = []
    for bundle in bundles:
        unit = units.get(bundle.unit_id)
        if unit is None:
            continue
        for item in bundle.evidence:
            source = item.full_text or item.content
            if not source:
                continue
            span = select_evidence_span(unit, source)
            if not span:
                continue
            item.evidence_span = span
            item.content = span
            item.fact_hints = extract_fact_hints(span)
            item.full_text = None
            if item.evidence_id not in touched:
                touched.append(item.evidence_id)
        if bundle.evidence and bundle.resolution.status in {"resolved", "partial"}:
            # Value is a bounded evidence preview, never a derived task state
            # or an implicit conflict decision.
            bundle.resolution.value = "\n".join(
                item.evidence_span or item.content for item in bundle.evidence[:2]
            )[:1000] or None
    return touched
