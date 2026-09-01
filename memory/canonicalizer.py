"""文件作用：稳定身份和 canonical key。

项目关系：本文件依赖 `memory.models`、`memory.policies.preference`；被 `memory.candidate_retriever`、`memory.candidate_validator`、`memory.extractor`、`memory.relation_guard` 等 9 个模块。
"""



from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any

from memory.models import MEMORY_KEY_V3_VERSION, MemoryCandidate, normalize_content
from memory.policies.preference import preference_signature
from memory.field_contracts import (
    episodic_topic,
    normalize_entity,
    normalize_operation,
    normalize_semantic_attribute,
    normalize_task_status,
    preference_topic_with_source,
    semantic_topic,
    task_attribute,
    task_closure_reason,
    task_progress_metadata,
)


_TASK_OPERATION_ALIASES = {
    "学习": "学习",
    "学完": "学习",
    "学": "学习",
    "换成": "更换",
    "更换": "更换",
    "换": "更换",
    "替换": "更换",
    "迁移": "迁移",
    "制作": "制作",
    "做完": "制作",
    "做": "制作",
    "开发": "开发",
    "实现": "实现",
    "修复": "修复",
    "完善": "完善",
    "修改": "修改",
    "处理": "处理",
    "维护": "维护",
    "执行": "执行",
    "部署": "部署",
    "发布": "发布",
}
_TASK_STATUS_WORDS = (
    "记得", "需要", "待办", "正在", "进行中", "已经", "完成", "完成了", "做完", "学完", "已学完", "取消", "阻塞",
)
_GENERIC_TASK_ATTRIBUTES = {"task", "任务", "todo", "fact", "事项", "当前任务", "当前学习主题", "待办"}
_TASK_IDENTITY_NOISE = (
    "当前学习主题", "当前任务", "已经", "正在", "进行中", "需要", "记得", "待办",
    "完成了", "完成", "已完成", "做完", "已做完", "搞定", "弄好了", "弄好", "取消",
    "不用做", "不做了", "任务", "事项", "制作", "执行", "维护", "学习", "更换", "迁移",
    "开发", "实现", "修复", "完善", "修改", "部署", "发布", "继续", "准备", "计划",
    "的", "也", "了", "一下", "呢", "啊", "呀", "哦", "啦",
)
_TASK_IDENTITY_PREFIX_RE = re.compile(
    r"^(?:继续(?:处理|做)?|正在(?:处理|做)?|开始(?:处理|做)?|处理|维护|执行|完成|做完|修复|完善|测试|评测|部署|发布|开发|实现|迁移|更换|替换|修改|整理|审查)+"
)
_TASK_IDENTITY_PROGRESS_SUFFIX_RE = re.compile(
    r"(?:第[一二三四五六七八九十\d]+(?:轮|阶段|次)|[一二三四五六七八九十\d]+(?:轮|阶段|次))$"
)
_TASK_IDENTITY_DETAIL_BOUNDARY_RE = re.compile(
    r"(?:现在|目前|当前)?(?:被|因|由于|卡在|阻塞|等待).*$"
)
_PURE_TASK_REFERENCE_RE = re.compile(
    r"^(?:这个|那个|它|这件事|上面那个)(?:(?:也)?(?:做完了?|完成了?|继续(?:做|处理)?(?:吧)?|取消了?|不做了?|不用做了?))?$"
)
_TASK_IDENTITY_ACTION_WORDS = frozenset(_TASK_OPERATION_ALIASES)
def normalize_identity(value: str | None, *, fallback: str = "unspecified") -> str:
    """函数功能：`normalize_identity` 负责归一化 identity，服务于本文件职责：稳定身份和 canonical key。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
        fallback: fallback 参数，由调用方传入，类型为 `str`，默认值为 `'unspecified'`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    normalized = normalize_content(value or "")
    return normalized[:120] or fallback


def normalize_scope(value: str | None) -> str:
    """函数功能：`normalize_scope` 负责归一化 scope，服务于本文件职责：稳定身份和 canonical key。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    normalized = normalize_identity(value, fallback="global")
    return "current" if normalized in {"当前", "current", "现在", "目前"} else normalized


def _task_identity_topic(value: str | None) -> str:
    """函数功能：`_task_identity_topic` 负责处理 task identity topic，服务于本文件职责：稳定身份和 canonical key。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    normalized = normalize_content(value or "")
    # Blockers and progress describe the lifecycle of a task, not the task
    # itself.  Keep the business goal stable when a user says the same work
    # is blocked, resumed, or in a numbered pass.
    normalized = _TASK_IDENTITY_DETAIL_BOUNDARY_RE.sub("", normalized)
    normalized = _TASK_IDENTITY_PREFIX_RE.sub("", normalized)
    normalized = _TASK_IDENTITY_PROGRESS_SUFFIX_RE.sub("", normalized)
    for marker in sorted(_TASK_IDENTITY_NOISE, key=len, reverse=True):
        # Action words have already been removed only when they are a leading
        # surface verb.  Removing them everywhere turns the noun phrase
        # “数据库迁移” into “数据库”, which is an unsafe family identity.
        if marker in _TASK_IDENTITY_ACTION_WORDS:
            continue
        normalized = normalized.replace(normalize_content(marker), "")
    return normalized


def _task_identity_topics(value: Any) -> list[str]:
    """Return grounded, stable Task-goal projections for compatibility checks."""
    scope = getattr(value, "scope", {}) or {}
    raw_topics = (
        getattr(value, "predicate", None),
        scope.get("canonical_topic"),
        getattr(value, "object_value", None),
    )
    topics = [_task_identity_topic(str(item or "")) for item in raw_topics]
    return list(dict.fromkeys(topic for topic in topics if topic and topic not in _GENERIC_TASK_ATTRIBUTES))


def task_family_compatible(candidate: Any, memory: Any) -> bool:
    """Whether two Task records are related enough for bounded recall.

    Family compatibility is deliberately broader than an instance identity.
    It may be used for retrieval, but never authorizes a task mutation.
    """
    if getattr(candidate, "memory_type", None) != "task" or getattr(memory, "memory_type", None) != "task":
        return False
    candidate_scope = normalize_scope(str((getattr(candidate, "scope", {}) or {}).get("scope") or "global"))
    memory_scope = normalize_scope(str((getattr(memory, "scope", {}) or {}).get("scope") or "global"))
    if candidate_scope != memory_scope:
        return False

    candidate_family = str((getattr(candidate, "scope", {}) or {}).get("task_family_key") or "")
    memory_family = str((getattr(memory, "scope", {}) or {}).get("task_family_key") or "")
    if candidate_family and candidate_family == memory_family:
        return True

    candidate_topics = _task_identity_topics(candidate)
    memory_topics = _task_identity_topics(memory)
    for left in candidate_topics:
        for right in memory_topics:
            if left == right:
                return True
            shorter, longer = sorted((left, right), key=len)
            # A short two-character Chinese fragment (for example “测试”) is
            # too broad to bridge identities.  Three characters is enough for
            # useful titles such as “数据库” while avoiding that false merge.
            if len(shorter) >= 3 and shorter in longer:
                return True
    return False


def task_instance_authorized(candidate: Any, memory: Any) -> bool:
    """Return whether deterministic Task mutation may target ``memory``.

    Lifecycle verbs differ between a reminder and its completion (for example
    ``完善 README`` / ``完成 README``), so operation is not an instance
    boundary. Explicit round/version identifiers are boundaries, however:
    ``第一轮`` and ``第二轮`` may share a Family but are never interchangeable.
    """
    if getattr(candidate, "memory_type", None) != "task" or getattr(memory, "memory_type", None) != "task":
        return False
    candidate_scope = normalize_scope(str((getattr(candidate, "scope", {}) or {}).get("scope") or "global"))
    memory_scope = normalize_scope(str((getattr(memory, "scope", {}) or {}).get("scope") or "global"))
    if candidate_scope != memory_scope:
        return False
    candidate_key = getattr(candidate, "effective_memory_key", "")
    memory_key = getattr(memory, "effective_memory_key", "")
    if candidate_key and candidate_key == memory_key:
        return True

    def instance_markers(value: Any) -> set[str]:
        raw = " ".join(
            str(item or "")
            for item in (
                getattr(value, "content", None),
                getattr(value, "predicate", None),
                getattr(value, "object_value", None),
            )
        )
        markers = set(re.findall(r"第[一二三四五六七八九十\d]+(?:轮|阶段|次)", raw))
        markers.update(match.casefold() for match in re.findall(r"\b[A-Za-z]+[-_]?\d+(?:\.\d+)?\b", raw))
        return markers

    candidate_markers = instance_markers(candidate)
    memory_markers = instance_markers(memory)
    if candidate_markers != memory_markers and (candidate_markers or memory_markers):
        return False
    candidate_topics = set(_task_identity_topics(candidate))
    memory_topics = set(_task_identity_topics(memory))
    if candidate_topics & memory_topics:
        return True
    # Legacy task rows often keep a wider family key or a shorter title while
    # the completion note carries the lifecycle transition.  When the task
    # family is clearly the same and the state actually changed, allow the
    # narrower mutation bridge; same-status rows still stay distinct.
    if getattr(candidate, "task_status", None) != getattr(memory, "task_status", None) and task_family_compatible(candidate, memory):
        return True
    if normalize_content(str(getattr(candidate, "subject", "") or "")) != normalize_content(str(getattr(memory, "subject", "") or "")):
        return False
    # A later, strictly longer title ending in the earlier title is a narrow
    # deterministic refinement ("简历" -> "Agent 开发的简历"), not a Family
    # match. The inverse direction and arbitrary fuzzy overlap remain denied.
    for candidate_topic in candidate_topics:
        for memory_topic in memory_topics:
            if len(memory_topic) >= 2 and len(candidate_topic) > len(memory_topic) and candidate_topic.endswith(memory_topic):
                return True
    return False


# Compatibility name retained for external callers. Internal mutation paths
# use ``task_instance_authorized`` explicitly.
task_identity_compatible = task_family_compatible


def _first_operation(text: str, supplied: str | None = None) -> str:
    """函数功能：`_first_operation` 负责处理 first operation，服务于本文件职责：稳定身份和 canonical key。
    传参：
        text: 输入文本内容，类型为 `str`。
        supplied: supplied 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    supplied_normalized = normalize_identity(supplied, fallback="")
    # 完成、已经完成等描述的是任务状态，不是任务身份。
    # 模型可能把它误放入 operation；这里忽略该槽位，并改用源文本或中性 fallback。
    if supplied_normalized in {"完成", "完成了", "已完成", "做完", "已做完"}:
        supplied_normalized = ""
    if supplied_normalized:
        for raw, canonical in _TASK_OPERATION_ALIASES.items():
            if raw in supplied or canonical in supplied:
                return canonical
        return supplied_normalized
    for marker, canonical in _TASK_OPERATION_ALIASES.items():
        if marker in text:
            return canonical
    return "维护"


_TASK_SURFACE_OPERATIONS = tuple(
    sorted(
        {
            *(_TASK_OPERATION_ALIASES.keys()),
            "完成",
        },
        key=len,
        reverse=True,
    )
)


def _task_action_and_material(text: str) -> tuple[str | None, str | None]:
    """函数功能：`_task_action_and_material` 负责处理 task action and material，服务于本文件职责：稳定身份和 canonical key。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `tuple[str | None, str | None]`，表示由多个相关值组成的结果。
    """
    value = re.sub(r"\s+", "", str(text or ""))
    if not value:
        return None, None
    operations = "|".join(re.escape(item) for item in _TASK_SURFACE_OPERATIONS)
    match = re.search(
        rf"(?P<operation>{operations})(?:了|好|一下)?(?P<material>[^，。！？!?；;、]+)",
        value,
    )
    if match is None:
        return None, None
    surface_operation = match.group("operation")
    material = match.group("material")
    material = re.sub(r"^(?:了|好|一下|去|将|给|把)+", "", material)
    material = re.sub(r"(?:呢|啊|呀|了|哦|啦)+$", "", material)
    material = material.strip(" ：:，。！？!?；;、")
    # Do not read an action embedded in a task title as a new imperative.
    # “数据库迁移已经做完” describes the task “数据库迁移”; treating
    # “迁移” as the leading action would leave only “已经做完” as material
    # and make a second task identity.
    if re.fullmatch(r"(?:已经|已|正在|进行中|做完|完成|完成了|提交|上线|取消|不做了|不用做|放弃)+", material):
        return None, None
    if len(normalize_identity(material, fallback="")) < 2:
        return None, None
    operation = "执行" if surface_operation == "完成" else _TASK_OPERATION_ALIASES.get(surface_operation, surface_operation)
    return operation, material[:80]


def _task_entity(value: str | None, *, extractor_type: str) -> str:
    """函数功能：`_task_entity` 负责处理 task entity，服务于本文件职责：稳定身份和 canonical key。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
        extractor_type: extractor type 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    del value, extractor_type
    # A task memory belongs to the current user/space.  Model-provided
    # "entity" is frequently a task noun (for example “评测”), not an owner;
    # using it in the key causes equivalent updates to split into new tasks.
    return "用户"


def _attribute_from_canonical_topic(topic: str, *, entity: str, operation: str) -> str:
    """函数功能：`_attribute_from_canonical_topic` 负责处理 attribute from canonical topic，服务于本文件职责：稳定身份和 canonical key。
    传参：
        topic: topic 参数，由调用方传入，类型为 `str`。
        entity: entity 参数，由调用方传入，类型为 `str`。
        operation: operation 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    value = re.sub(r"\s+", "", str(topic or ""))
    if not value:
        return ""
    operation_markers = sorted({operation, *(_TASK_OPERATION_ALIASES.keys())}, key=len, reverse=True)
    for marker in operation_markers:
        if marker and value.startswith(marker):
            value = value[len(marker) :]
            break
    if entity and entity not in {"用户", "我", "本人"} and value.startswith(entity):
        value = value[len(entity) :]
    return value.strip("的：: ，。！？!?；;、")[:80]


def _learning_attribute(text: str) -> str | None:
    """函数功能：`_learning_attribute` 负责处理 learning attribute，服务于本文件职责：稳定身份和 canonical key。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    match = re.search(r"(?:学习|学完|学)\s*([^，。！？!?；;、]{1,40})", re.sub(r"\s+", "", text))
    if match is None:
        return None
    value = match.group(1).strip("的：: ，。！？!?；;、")
    value = re.sub(r"(?:呢|啊|呀|了|哦|啦)+$", "", value)
    return value[:80] or None


def _task_identity_from_text(text: str, candidate: MemoryCandidate) -> tuple[str, str, str]:
    """函数功能：`_task_identity_from_text` 负责处理 task identity from text，服务于本文件职责：稳定身份和 canonical key。
    传参：
        text: 输入文本内容，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
    返回结果说明：
        返回 `tuple[str, str, str]`，表示由多个相关值组成的结果。
    """
    entity = _task_entity(candidate.subject, extractor_type=candidate.extractor_type)
    attribute = str(candidate.predicate or "").strip()
    operation = str(candidate.scope.get("operation") or "").strip()
    resolved_operation = _first_operation(text, operation)
    canonical_topic = str(candidate.scope.get("canonical_topic") or "").strip()
    compact_text = re.sub(r"\s+", "", str(text or "")).strip("。！？!?；;")
    reference_is_identity_incomplete = bool(_PURE_TASK_REFERENCE_RE.fullmatch(compact_text))
    if candidate.scope.get("reference_status") == "resolved" and canonical_topic and reference_is_identity_incomplete:
        # A resolved demonstrative (“这个也做完了”) inherits the antecedent
        # identity; its surface wording must not create a topic called “这个也”.
        inherited = _attribute_from_canonical_topic(canonical_topic, entity=entity, operation=resolved_operation)
        if inherited:
            return entity, inherited, str(candidate.scope.get("operation") or resolved_operation or "维护")
    text_operation, text_attribute = _task_action_and_material(text)
    current_assertion_with_context = candidate.scope.get("reference_status") == "resolved" and not reference_is_identity_incomplete
    if current_assertion_with_context:
        # Resolved context is a relationship, not proof that an explicitly
        # named current assertion has the antecedent's exact identity. Model
        # fields copied from context are replaced by current evidence; the
        # caller retains the antecedent only as a retrieval-only family link.
        text_attribute = re.sub(
            r"^(?:我|本人|用户)", "", task_attribute(None, text) or text_attribute
        ).strip(" ：:，,。！？!?；;") or text_attribute
    progress_match = re.match(r"^(.*?)(?:正在(?:修复失败用例|补充测试|处理)|继续处理)(?:[^，。！？!?；;]*)$", text)
    if progress_match and progress_match.group(1).strip():
        text_attribute = progress_match.group(1).strip(" ：:，。！？!?；;")
        text_operation = "执行"

    # 第一人称任务陈述拥有明确所有者；不要让 LLM 在一次输出中把任务材料本身当作 entity，又在下一次输出中只取更短后缀。
    if re.match(r"\s*(?:我|本人|用户)", str(text or "")):
        entity = "用户"
    if "随心记" in text:
        entity = "随心记"

    if any(marker in text for marker in ("大模型", "模型")) and "供应商" in text:
        attribute = "大模型供应商"
    elif current_assertion_with_context and text_attribute:
        # Do not let an antecedent canonical topic overwrite the explicitly
        # present material merely because this clause has no surface verb.
        attribute = text_attribute
    elif text_operation and text_attribute:
        # 显式源证据优先于不一致的模型槽位，使“需要完成 X / 正在完成 X / 已经完成 X”归为同一任务，
        # 而不是产生三条无关记忆。
        resolved_operation = text_operation
        attribute = text_attribute
        if entity == "随心记":
            attribute = re.sub(r"^随心记(?:项目)?(?:的)?", "", attribute) or attribute
    elif canonical_topic:
        # schema 明确区分 canonical topic 和展示标签；始终优先使用派生任务材料，避免模型换了属性标签就创建新任务。
        attribute = _attribute_from_canonical_topic(canonical_topic, entity=entity, operation=resolved_operation) or attribute
    elif _learning_attribute(text):
        attribute = _learning_attribute(text) or attribute
    elif not attribute or normalize_identity(attribute) in _GENERIC_TASK_ATTRIBUTES:
        cleaned = re.sub(r"(?:记得|需要|待办|正在|已经|完成|做完|给|把|将|的|了|从|成|为)", "", text)
        for marker in _TASK_OPERATION_ALIASES:
            cleaned = cleaned.replace(marker, "")
        attribute = cleaned.strip(" ，。！？!?；;：:")[:48] or "任务"

    # “发布思维导图版本这个任务已经完成” has one stable task goal
    # (“发布思维导图版本”) and a trailing lifecycle assertion.  The latter
    # belongs to task_status / closure_reason and must not pollute identity.
    attribute = re.sub(
        r"(?:这个|该)?(?:任务|事项)(?:已经|已)?(?:完成|做完|搞定|取消|不做了|不用做|放弃)+$",
        "",
        attribute,
    ).strip(" ：:，。！？!?；;") or attribute
    attribute = re.sub(r"(?:这个|该)?(?:任务|事项)$", "", attribute).strip(" ：:，。！？!?；;") or attribute

    return entity, attribute, resolved_operation


def _task_values(text: str, scope: dict[str, Any]) -> tuple[str | None, str | None]:
    """函数功能：`_task_values` 负责处理 task values，服务于本文件职责：稳定身份和 canonical key。
    传参：
        text: 输入文本内容，类型为 `str`。
        scope: scope 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `tuple[str | None, str | None]`，表示由多个相关值组成的结果。
    """
    old_value = str(scope.get("old_value") or "").strip() or None
    new_value = str(scope.get("new_value") or "").strip() or None
    if old_value or new_value:
        return old_value, new_value

    replacement = re.search(r"从\s*([^，。！？!?；;]+?)\s*(?:换成|更换为|改成|替换成)\s*([^，。！？!?；;]+)", text)
    if replacement:
        return replacement.group(1).strip(), replacement.group(2).strip().rstrip("了") or None
    in_progress = re.search(r"(?:换|更换|替换)\s*([A-Za-z][A-Za-z0-9._-]*)\s*(?:供应商|模型)?", text)
    if in_progress:
        return None, in_progress.group(1).strip()
    return None, None


def task_key(entity: str, attribute: str, operation: str, scope: str = "global") -> str:
    """函数功能：`task_key` 负责处理 task key，服务于本文件职责：稳定身份和 canonical key。
    传参：
        entity: entity 参数，由调用方传入，类型为 `str`。
        attribute: attribute 参数，由调用方传入，类型为 `str`。
        operation: operation 参数，由调用方传入，类型为 `str`。
        scope: scope 参数，由调用方传入，类型为 `str`，默认值为 `'global'`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    entity_key = "用户" if str(entity or "").strip() in {"", "我", "本人", "用户", "user", "me"} else normalize_identity(entity)
    return ":".join(
        (
            "task",
            entity_key,
            normalize_identity(attribute),
            normalize_identity(operation),
            normalize_scope(scope),
        )
    )


def semantic_key(entity: str, attribute: str, fact_text: str, scope: str = "current") -> str:
    """Build an append-only key for one semantic assertion."""
    entity_key = "用户" if str(entity or "").strip() in {"", "我", "本人", "用户", "user", "me"} else normalize_identity(entity)
    attribute_key = normalize_identity(attribute)
    digest = hashlib.sha256(normalize_identity(fact_text).encode("utf-8")).hexdigest()[:16]
    return f"semantic:{entity_key}:{attribute_key}:{digest}"


def preference_key(entity: str, topic: str, scope: str = "global", *, qualifiers: tuple[str, ...] | list[str] = ()) -> str:
    """函数功能：`preference_key` 负责处理 preference key，服务于本文件职责：稳定身份和 canonical key。
    传参：
        entity: entity 参数，由调用方传入，类型为 `str`。
        topic: topic 参数，由调用方传入，类型为 `str`。
        scope: scope 参数，由调用方传入，类型为 `str`，默认值为 `'global'`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    entity_key = "用户" if str(entity or "").strip() in {"", "我", "本人", "用户", "user", "me"} else normalize_identity(entity)
    key_parts = ["preference", entity_key, normalize_identity(topic), normalize_scope(scope)]
    key_parts.extend(normalize_identity(item) for item in qualifiers if normalize_identity(item))
    return ":".join(key_parts)


def task_family_key(topic: str) -> str | None:
    """Project a query topic into the same retrieval-only Task family space."""
    identity = _task_identity_topic(topic)
    if not identity:
        return None
    return f"task-family:{identity}"


def preference_family_key(entity: str, topic: str, source_text: str | None = None) -> str:
    """Build a retrieval-only Preference family key from a business class."""
    from memory.policies.preference import preference_family

    entity_key = "用户" if str(entity or "").strip() in {"", "我", "本人", "用户", "user", "me"} else normalize_identity(entity)
    return f"preference-family:{entity_key}:{normalize_identity(preference_family(topic, source_text))}"


def is_task_lifecycle_statement(text: str) -> bool:
    """函数功能：`is_task_lifecycle_statement` 负责判断是否为 task lifecycle statement，服务于本文件职责：稳定身份和 canonical key。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    raw = str(text or "")
    # “正在学习/研究 X” is a current learning focus (semantic) unless
    # the sentence explicitly frames learning as a task or commitment.
    if "学习" in raw and not any(marker in raw for marker in ("记得", "需要", "待办", "任务", "计划", "准备", "完成", "做完")):
        return False
    return any(word in raw for word in _TASK_STATUS_WORDS) and any(
        operation in raw for operation in _TASK_OPERATION_ALIASES
    )


def canonicalize_candidate(candidate: MemoryCandidate) -> MemoryCandidate:
    """函数功能：`canonicalize_candidate` 负责处理 canonicalize candidate，服务于本文件职责：稳定身份和 canonical key。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
    返回结果说明：
        返回 `MemoryCandidate` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    scope = dict(candidate.scope)
    source_text = candidate.evidence_span or candidate.content
    memory_type = candidate.memory_type

    if memory_type == "task":
        compact_source = re.sub(r"\s+", "", str(source_text or "")).strip("。！？!?；;")
        resolved_reference = scope.get("reference_status") == "resolved"
        identity_from_antecedent = resolved_reference and bool(_PURE_TASK_REFERENCE_RE.fullmatch(compact_source))
        if identity_from_antecedent:
            scope["identity_source"] = "antecedent"
        else:
            scope["identity_source"] = "current_evidence"
            if resolved_reference:
                related_family = str(
                    scope.get("related_task_family_key") or scope.get("task_family_key") or ""
                ).strip()
                if related_family:
                    scope["related_task_family_key"] = related_family
                # A current assertion with its own text receives a new exact
                # identity. The old family is non-authorizing retrieval context.
                scope.pop("task_family_key", None)
        scope.update(task_progress_metadata(source_text))
        closure_reason = task_closure_reason(source_text=source_text)
        if normalize_task_status(candidate.task_status, source_text) == "done" and closure_reason:
            scope["closure_reason"] = closure_reason
        entity, attribute, operation = _task_identity_from_text(source_text, replace(candidate, scope=scope))
        entity = normalize_entity(entity, memory_type="task") or "用户"
        attribute = task_attribute(attribute, source_text) or attribute or "任务"
        operation = normalize_operation(operation, source_text) or operation or "执行"
        old_value, new_value = _task_values(source_text, scope)
        topic_entity = "" if entity == "用户" else entity
        # 同时持久化确定性 identity projection；若保留不一致的模型 canonical_topic，后续对账和诊断会重新出现同样漂移。
        canonical_topic = f"{operation}{topic_entity}{attribute}"
        canonical_scope = str(scope.get("scope") or "global")
        scope.update(
            {
                "canonical_topic": canonical_topic,
                "operation": operation,
                "scope": canonical_scope,
                "old_value": old_value,
                "new_value": new_value,
                "task_status": normalize_task_status(candidate.task_status, source_text) or "todo",
                "memory_key_version": MEMORY_KEY_V3_VERSION,
                "task_family_key": scope.get("task_family_key")
                if scope.get("identity_source") == "antecedent" and scope.get("task_family_key")
                else f"task-family:{_task_identity_topic(f'{topic_entity}{attribute}') or _task_identity_topic(attribute) or normalize_identity(attribute)}",
            }
        )
        return replace(
            candidate,
            subject=entity,
            task_status=normalize_task_status(candidate.task_status, source_text) or "todo",
            predicate=attribute,
            object_value=new_value or candidate.object_value or attribute,
            memory_key=task_key(entity, attribute, operation, canonical_scope),
            scope=scope,
            memory_key_version=MEMORY_KEY_V3_VERSION,
        )

    if memory_type == "preference":
        signature = preference_signature(source_text)
        polarity = candidate.polarity if candidate.polarity in {"positive", "negative", "unknown"} else signature.polarity
        entity = normalize_entity(candidate.subject, memory_type="preference") or "用户"
        topic, derived_topic_source = preference_topic_with_source(
            source_text,
            scope.get("canonical_topic"),
            candidate.object_value,
        )
        topic = topic or signature.topic or source_text
        topic_source = str(scope.get("topic_source") or "").strip() or derived_topic_source
        explicit_scope = bool(scope.get("scope_explicit"))
        canonical_scope = str(scope.get("scope") or "").strip()
        if not explicit_scope:
            canonical_scope = canonical_scope if canonical_scope and canonical_scope != "global" else (signature.scopes[0] if signature.scopes else "global")
        canonical_scope = normalize_scope(canonical_scope)
        scope["scope"] = canonical_scope
        scope["scope_explicit"] = canonical_scope != "global"
        scope["scope_source"] = scope.get("scope_source") or ("rules" if canonical_scope != "global" else "default")
        from memory.policies.preference import preference_qualifiers

        assertion_qualifiers = list(preference_qualifiers(source_text, scope.get("qualifiers")))
        qualifiers = list(dict.fromkeys([*signature.scopes, *assertion_qualifiers]))
        if any(marker in source_text for marker in ("更喜欢", "更偏好", "更偏向")) and "更偏向" not in qualifiers:
            qualifiers.append("更偏向")
        scope.update(
            {
                "canonical_topic": topic,
                "topic_source": topic_source or "rules",
                "scope": canonical_scope,
                "memory_key_version": MEMORY_KEY_V3_VERSION,
                "preference_family_key": preference_family_key(entity, topic, source_text),
                "preference_assertion_key": preference_key(entity, topic, canonical_scope, qualifiers=assertion_qualifiers),
                "polarity": polarity,
                "qualifiers": qualifiers,
            }
        )
        return replace(
            candidate,
            subject=entity,
            predicate="preference",
            object_value=topic,
            polarity=polarity,
            memory_key=preference_key(entity, topic, canonical_scope, qualifiers=assertion_qualifiers),
            scope=scope,
            memory_key_version=MEMORY_KEY_V3_VERSION,
        )

    if memory_type == "semantic":
        entity = normalize_entity(candidate.subject, memory_type="semantic") or "用户"
        raw_attribute = str(candidate.predicate or "").strip()
        attribute = normalize_semantic_attribute(raw_attribute, source_text) or "other"
        canonical_topic = semantic_topic(attribute, scope.get("canonical_topic")) or source_text
        canonical_scope = str(scope.get("scope") or "current")
        scope.update(
            {
                "canonical_topic": canonical_topic,
                "scope": canonical_scope,
                "semantic_append_only": True,
                "memory_key_version": MEMORY_KEY_V3_VERSION,
            }
        )
        return replace(
            candidate,
            subject=entity,
            predicate=attribute,
            memory_key=semantic_key(entity, attribute, source_text, canonical_scope),
            scope=scope,
            memory_key_version=MEMORY_KEY_V3_VERSION,
        )

    if memory_type == "episodic":
        entity = normalize_entity(candidate.subject, memory_type="episodic") or "用户"
        topic = episodic_topic(source_text, scope.get("canonical_topic"), candidate.object_value) or source_text
        canonical_scope = str(scope.get("scope") or "history")
        scope.update(
            {
                "canonical_topic": topic,
                "scope": canonical_scope,
                "memory_key_version": MEMORY_KEY_V3_VERSION,
            }
        )
        return replace(
            candidate,
            subject=entity,
            predicate="event",
            object_value=topic,
            memory_key=f"episodic:{'用户' if entity == '用户' else normalize_identity(entity)}:event:{normalize_identity(topic)}",
            scope=scope,
            memory_key_version=MEMORY_KEY_V3_VERSION,
        )

    scope.update({"memory_key_version": MEMORY_KEY_V3_VERSION})
    return replace(candidate, scope=scope, memory_key_version=MEMORY_KEY_V3_VERSION)


def normalize_candidate_v3(candidate: MemoryCandidate, note_text: str | None = None) -> MemoryCandidate:
    """Normalize every extractor output through one deterministic contract."""
    del note_text
    return canonicalize_candidate(candidate)
