"""文件作用：偏好策略。

项目关系：本文件依赖 `memory.models`；被 `eval.eval_memory_quality`、`memory.canonicalizer`、`memory.extractor`、`memory.retriever` 等 5 个模块。
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
    "避开",
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
    "偏向",
    "更偏向",
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
# 保留型号编码和独立版本号作为锚点；它们比周围中文语境更具体（如 X1/X10、iPhone 15/iPhone 16），不能被模糊子串匹配抹平。
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
    """类功能：`PreferenceSignature` 封装与“偏好策略”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    topic: str
    normalized_topic: str
    polarity: str
    scopes: tuple[str, ...]
    qualifiers: tuple[str, ...]
    named_anchors: tuple[str, ...]


def preference_polarity(text: str) -> str:
    """函数功能：`preference_polarity` 负责处理 preference polarity，服务于本文件职责：偏好策略。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    value = str(text or "")
    negative_positions = [value.find(marker) for marker in NEGATIVE_MARKERS if marker in value]
    negative_positions.extend(match.start() for match in _NEGATIVE_ACTION_RE.finditer(value))
    positive_positions = [value.find(marker) for marker in POSITIVE_MARKERS if marker in value]
    if negative_positions and (not positive_positions or min(negative_positions) <= min(positive_positions)):
        return "negative"
    if positive_positions:
        return "positive"
    return "unknown"


def preference_query_polarity(text: str) -> str:
    """函数功能：`preference_query_polarity` 负责查询 polarity，服务于本文件职责：偏好策略。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    value = " ".join(str(text or "").split()).strip()
    normalized = value.casefold()
    informational_preference = (
        any(marker in value for marker in ("偏好", "习惯"))
        and any(marker in value for marker in ("对应", "是什么", "怎么", "如何", "记录", "查", "找", "最近", "关于"))
    )
    interrogative = (
        "?" in value
        or "？" in value
        or informational_preference
        or bool(re.search(r"(?:我|用户)?(?:是否|是不是|有没有)", value))
        or bool(re.search(r"(?:哪种|哪个|什么偏好|偏好是什么|习惯是什么|吗|嘛|呢)$", value.rstrip("。！!")))
        or normalized.startswith(("是否", "是不是", "do i ", "did i ", "what ", "which "))
    )
    return "unknown" if interrogative else preference_polarity(value)


def _extract_scopes(text: str) -> tuple[str, ...]:
    """函数功能：`_extract_scopes` 负责抽取 scopes，服务于本文件职责：偏好策略。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `tuple[str, ...]`，表示由多个相关值组成的结果。
    """
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
    """函数功能：`_strip_scope_prefix` 负责处理 strip scope prefix，服务于本文件职责：偏好策略。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    value = text.strip()
    for scope in sorted(_FIXED_SCOPES, key=len, reverse=True):
        if value.startswith(scope):
            value = value[len(scope) :].lstrip("，,：: ")
    value = re.sub(r"^(?:在|当)[^，,。；;]{1,16}?(?:时|的时候)", "", value).strip("，,：: ")
    return value


def _marker_and_remainder(text: str) -> tuple[str | None, str]:
    """函数功能：`_marker_and_remainder` 负责处理 marker and remainder，服务于本文件职责：偏好策略。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `tuple[str | None, str]`，表示由多个相关值组成的结果。
    """
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
    """函数功能：`_extract_topic` 负责抽取 topic，服务于本文件职责：偏好策略。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `tuple[str, tuple[str, ...]]`，表示由多个相关值组成的结果。
    """
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
    qualifier_values = list(qualifiers)
    if main.startswith("无糖") and len(main) > 2:
        main = main[2:].strip()
        qualifier_values.append("sugar_free")
    elif main.startswith("太甜的") and len(main) > 3:
        main = main[3:].strip()
        qualifier_values.append("sweet")
    # Collapse descriptive sugar wording to the stable preference topic;
    # qualifiers such as scope and polarity remain separate fields.
    main = {"太甜": "sweetness", "太甜的饮料": "甜味饮料", "甜的饮料": "甜味饮料"}.get(main, main)
    return main[:160], tuple(dict.fromkeys(qualifier_values))


def preference_signature(text: str, topic_hint: str | None = None) -> PreferenceSignature:
    """函数功能：`preference_signature` 负责处理 preference signature，服务于本文件职责：偏好策略。
    传参：
        text: 输入文本内容，类型为 `str`。
        topic_hint: topic hint 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `PreferenceSignature` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    topic, qualifiers = _extract_topic(text)
    hint = str(topic_hint or "").strip()
    # 只有确定性抽取没有找到具体对象时，模型提示才有用；这可以避免旧版句子级 object 字段重新引入泛化模板相似。
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
    """函数功能：`_bigrams` 负责处理 bigrams，服务于本文件职责：偏好策略。
    传参：
        value: 待转换、校验或计算的值，类型为 `str`。
    返回结果说明：
        返回 `set[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if len(value) < 2:
        return {value} if value else set()
    return {value[index : index + 2] for index in range(len(value) - 1)}


def topic_compatibility(left: Any, right: Any) -> float:
    """函数功能：`topic_compatibility` 负责处理 topic compatibility，服务于本文件职责：偏好策略。
    传参：
        left: left 参数，由调用方传入，类型为 `Any`。
        right: right 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
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
        # 显式编码、产品名和版本号是具体主题证据；必须完整集合相同才认为两个主题兼容，否则 X1 这类短值可能因成为无关 X10 的子串而误触发变更。
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
    """函数功能：`scopes_compatible` 负责处理 scopes compatible，服务于本文件职责：偏好策略。
    传参：
        left: left 参数，由调用方传入，类型为 `Any`。
        right: right 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    left_scopes = set(preference_signature(str(getattr(left, "content", "") or "")).scopes)
    right_scopes = set(preference_signature(str(getattr(right, "content", "") or "")).scopes)
    if not left_scopes or not right_scopes:
        return True
    return bool(left_scopes & right_scopes)


def has_negation(text: str) -> bool:
    """函数功能：`has_negation` 负责判断是否包含 negation，服务于本文件职责：偏好策略。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return preference_polarity(text) == "negative"


def is_ambiguous_conflict(new_content: str, old_content: str) -> bool:
    """函数功能：`is_ambiguous_conflict` 负责判断是否为 ambiguous conflict，服务于本文件职责：偏好策略。
    传参：
        new_content: new content 参数，由调用方传入，类型为 `str`。
        old_content: old content 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return is_comparative_alternative(new_content, old_content)


def _common_suffix_length(left: str, right: str) -> int:
    """函数功能：`_common_suffix_length` 负责处理 common suffix length，服务于本文件职责：偏好策略。
    传参：
        left: left 参数，由调用方传入，类型为 `str`。
        right: right 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    count = 0
    for left_char, right_char in zip(reversed(left), reversed(right)):
        if left_char != right_char:
            break
        count += 1
    return count


def is_comparative_alternative(new_content: str, old_content: str) -> bool:
    """函数功能：`is_comparative_alternative` 负责判断是否为 comparative alternative，服务于本文件职责：偏好策略。
    传参：
        new_content: new content 参数，由调用方传入，类型为 `str`。
        old_content: old content 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
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
    """函数功能：`explicitly_replaces` 负责处理 explicitly replaces，服务于本文件职责：偏好策略。
    传参：
        new_content: new content 参数，由调用方传入，类型为 `str`。
        old_content: old content 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    new_polarity = preference_polarity(new_content)
    old_polarity = preference_polarity(old_content)
    polarity_changed = new_polarity != "unknown" and old_polarity != "unknown" and new_polarity != old_polarity
    return polarity_changed or any(marker in new_content for marker in CHANGE_MARKERS)
