"""Deterministic preference topic, polarity, and scope policy.

Whole-sentence similarity is intentionally not used as permission to mutate an
existing preference.  A candidate must first share a concrete preference topic
with the target; generic templates such as "用户喜欢" carry no topic evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from memory.models import normalize_content


NEGATIVE_MARKERS = (
    "不再喜欢",
    "不再",
    "不喜欢",
    "不想要",
    "不想",
    "不打算",
    "不愿意",
    "不愿",
    "不要",
    "不爱",
    "讨厌",
    "厌恶",
    "避免",
    "拒绝",
    "过敏",
)
POSITIVE_MARKERS = (
    "更喜欢",
    "最喜欢",
    "喜欢",
    "偏好",
    "习惯",
    "想要",
    "倾向于",
    "优先选择",
    "优先",
)
CHANGE_MARKERS = ("现在", "以后", "改为", "改成", "不再", "从现在起", "目前", "最近")
COMPARATIVE_MARKERS = ("更喜欢", "更偏好", "相比", "相较", "宁愿", "而不是", "而非")

_LIGHT_VERB_RE = re.compile(r"^(?:吃|喝|用|使用|采用|选择|选|穿|看|听|玩|住|做|学习|学|买|去)+")
_NEGATIVE_ACTION_RE = re.compile(
    r"(?:暂时|目前|现在)?不(?:再)?(?=(?:吃|喝|用|使用|采用|选择|选|穿|看|听|玩|住|做|学习|学|买|去))"
)
_LEADING_OWNER_RE = re.compile(r"^(?:用户|本人|我)+")
_LEADING_CHANGE_RE = re.compile(r"^(?:现在|以后|目前|最近|从现在起|已经|改为|改成)+")
_TRAILING_PARTICLE_RE = re.compile(r"(?:了|啦|呢|吧|呀|啊)+$")
_CLAUSE_SPLIT_RE = re.compile(r"[，,；;。\n]|(?:但是|不过|同时|而且)")
# Keep model codes and standalone version numbers as anchors.  They are more
# specific than generic Chinese context around them (for example, X1 vs X10 or
# iPhone 15 vs iPhone 16) and must not be blurred by fuzzy substring matching.
_NAMED_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]*|\d+(?:[._-]\d+)*")
_GENERIC_SCOPE_RE = re.compile(r"(?:在|当)([^，,。；;]{1,16}?)(?:时|的时候)")
_FIXED_SCOPES = (
    "早上",
    "上午",
    "中午",
    "下午",
    "晚上",
    "夜里",
    "工作时",
    "学习时",
    "开会时",
    "周末",
    "工作日",
    "在家",
    "在公司",
    "在办公室",
)


@dataclass(frozen=True)
class PreferenceSignature:
    topic: str
    normalized_topic: str
    polarity: str
    scopes: tuple[str, ...]
    qualifiers: tuple[str, ...]
    named_anchors: tuple[str, ...]


def preference_polarity(text: str) -> str:
    value = str(text or "")
    negative_positions = [value.find(marker) for marker in NEGATIVE_MARKERS if marker in value]
    negative_positions.extend(match.start() for match in _NEGATIVE_ACTION_RE.finditer(value))
    positive_positions = [value.find(marker) for marker in POSITIVE_MARKERS if marker in value]
    if negative_positions and (not positive_positions or min(negative_positions) <= min(positive_positions)):
        return "negative"
    if positive_positions:
        return "positive"
    return "unknown"


def _extract_scopes(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for scope in _FIXED_SCOPES:
        if scope in text and scope not in found:
            found.append(scope)
    for match in _GENERIC_SCOPE_RE.finditer(text):
        scope = normalize_content(match.group(1))
        if scope and scope not in found:
            found.append(scope)
    return tuple(found[:6])


def _strip_scope_prefix(text: str) -> str:
    value = text.strip()
    for scope in sorted(_FIXED_SCOPES, key=len, reverse=True):
        if value.startswith(scope):
            value = value[len(scope) :].lstrip("，,：: ")
    value = re.sub(r"^(?:在|当)[^，,。；;]{1,16}?(?:时|的时候)", "", value).strip("，,：: ")
    return value


def _marker_and_remainder(text: str) -> tuple[str | None, str]:
    markers = sorted(NEGATIVE_MARKERS + POSITIVE_MARKERS, key=len, reverse=True)
    matches = [(text.find(marker), marker) for marker in markers if marker in text]
    matches.extend((match.start(), match.group(0)) for match in _NEGATIVE_ACTION_RE.finditer(text))
    if not matches:
        return None, text
    index, marker = min(matches, key=lambda item: item[0])
    if marker == "过敏":
        before = text[:index]
        match = re.search(r"(?:对)?([^，,。；;]{1,30})$", before)
        return marker, match.group(1) if match else before
    return marker, text[index + len(marker) :]


def _extract_topic(text: str) -> tuple[str, tuple[str, ...]]:
    value = " ".join(str(text or "").split()).strip()
    value = _LEADING_OWNER_RE.sub("", value).strip()
    value = _LEADING_CHANGE_RE.sub("", value).strip()
    marker, remainder = _marker_and_remainder(value)
    if marker is None:
        remainder = value

    parts = [part.strip() for part in _CLAUSE_SPLIT_RE.split(remainder) if part.strip()]
    main = parts[0] if parts else remainder
    qualifiers = tuple(parts[1:5])
    main = _strip_scope_prefix(main)
    main = _LEADING_CHANGE_RE.sub("", main).strip()
    main = _LIGHT_VERB_RE.sub("", main).strip(" ：:，,。；;")
    main = re.sub(r"^(?:对|对于)", "", main).strip()
    main = re.sub(r"(?:而不是|而非|胜过|多于).*$", "", main).strip()
    main = _TRAILING_PARTICLE_RE.sub("", main).strip()
    return main[:160], qualifiers


def preference_signature(text: str, topic_hint: str | None = None) -> PreferenceSignature:
    topic, qualifiers = _extract_topic(text)
    hint = str(topic_hint or "").strip()
    # Model-provided hints are useful only when deterministic extraction found no
    # concrete object.  This prevents legacy, sentence-sized object fields from
    # reintroducing generic template similarity.
    if not topic and hint:
        topic, _ = _extract_topic(hint)
    normalized = normalize_content(topic)
    named = tuple(dict.fromkeys(token.casefold() for token in _NAMED_TOKEN_RE.findall(topic)))
    return PreferenceSignature(
        topic=topic,
        normalized_topic=normalized,
        polarity=preference_polarity(text),
        scopes=_extract_scopes(text),
        qualifiers=qualifiers,
        named_anchors=named,
    )


def _bigrams(value: str) -> set[str]:
    if len(value) < 2:
        return {value} if value else set()
    return {value[index : index + 2] for index in range(len(value) - 1)}


def topic_compatibility(left: Any, right: Any) -> float:
    """Return a strict topic score independent from whole-sentence similarity."""
    left_sig = preference_signature(str(getattr(left, "content", "") or ""), getattr(left, "object_value", None))
    right_sig = preference_signature(str(getattr(right, "content", "") or ""), getattr(right, "object_value", None))
    left_topic = left_sig.normalized_topic
    right_topic = right_sig.normalized_topic
    if not left_topic or not right_topic:
        return 0.0
    if left_topic == right_topic:
        return 1.0
    left_named = set(left_sig.named_anchors)
    right_named = set(right_sig.named_anchors)
    if left_named or right_named:
        # Explicit codes, product names, and versions are concrete topic
        # evidence.  Require the same complete set before considering two
        # topics compatible; otherwise a shorter value such as X1 can be a
        # substring of the unrelated X10 and wrongly trigger a mutation.
        if left_named == right_named:
            return 0.94
        return 0.0

    shorter, longer = sorted((left_topic, right_topic), key=len)
    if len(shorter) >= 2 and shorter in longer and len(shorter) / len(longer) >= 0.55:
        return 0.88

    left_bigrams = _bigrams(left_topic)
    right_bigrams = _bigrams(right_topic)
    if not left_bigrams or not right_bigrams:
        return 0.0
    overlap = len(left_bigrams & right_bigrams) / len(left_bigrams | right_bigrams)
    return round(0.78 * overlap, 4) if overlap >= 0.58 else 0.0


def scopes_compatible(left: Any, right: Any) -> bool:
    left_scopes = set(preference_signature(str(getattr(left, "content", "") or "")).scopes)
    right_scopes = set(preference_signature(str(getattr(right, "content", "") or "")).scopes)
    if not left_scopes or not right_scopes:
        return True
    return bool(left_scopes & right_scopes)


def has_negation(text: str) -> bool:
    return preference_polarity(text) == "negative"


def is_ambiguous_conflict(new_content: str, old_content: str) -> bool:
    return is_comparative_alternative(new_content, old_content)


def _common_suffix_length(left: str, right: str) -> int:
    count = 0
    for left_char, right_char in zip(reversed(left), reversed(right)):
        if left_char != right_char:
            break
        count += 1
    return count


def is_comparative_alternative(new_content: str, old_content: str) -> bool:
    """Detect a general comparative choice within a shared noun context."""
    if not any(marker in new_content for marker in COMPARATIVE_MARKERS):
        return False
    if preference_polarity(new_content) != "positive" or preference_polarity(old_content) != "positive":
        return False
    new_topic = preference_signature(new_content).normalized_topic
    old_topic = preference_signature(old_content).normalized_topic
    if not new_topic or not old_topic or new_topic == old_topic:
        return False
    common_suffix = _common_suffix_length(new_topic, old_topic)
    return common_suffix >= 2 and common_suffix / min(len(new_topic), len(old_topic)) >= 0.4


def explicitly_replaces(new_content: str, old_content: str) -> bool:
    new_polarity = preference_polarity(new_content)
    old_polarity = preference_polarity(old_content)
    polarity_changed = new_polarity != "unknown" and old_polarity != "unknown" and new_polarity != old_polarity
    return polarity_changed or any(marker in new_content for marker in CHANGE_MARKERS)
