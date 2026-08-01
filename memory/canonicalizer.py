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
_STABLE_SEMANTIC_ATTRIBUTES = {
    "location": "居住地",
    "current_project": "当前项目",
    "currentproject": "当前项目",
    "current_employer": "当前雇主",
    "currentemployer": "当前雇主",
    "learning_focus": "学习重点",
    "learningfocus": "学习重点",
    "birthplace": "籍贯",
}


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
    for marker in sorted(_TASK_IDENTITY_NOISE, key=len, reverse=True):
        normalized = normalized.replace(normalize_content(marker), "")
    return normalized


def task_identity_compatible(candidate: Any, memory: Any) -> bool:
    """函数功能：`task_identity_compatible` 负责处理 task identity compatible，服务于本文件职责：稳定身份和 canonical key。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `Any`。
        memory: memory 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if getattr(candidate, "memory_type", None) != "task" or getattr(memory, "memory_type", None) != "task":
        return False
    candidate_scope = normalize_scope(str((getattr(candidate, "scope", {}) or {}).get("scope") or "global"))
    memory_scope = normalize_scope(str((getattr(memory, "scope", {}) or {}).get("scope") or "global"))
    if candidate_scope != memory_scope:
        return False

    def topic(value: Any) -> str:
        """函数功能：`topic` 负责处理 topic，服务于本文件职责：稳定身份和 canonical key。
        传参：
            value: 待转换、校验或计算的值，类型为 `Any`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        predicate = str(getattr(value, "predicate", None) or "")
        if normalize_content(predicate) not in {normalize_content(item) for item in _GENERIC_TASK_ATTRIBUTES} and predicate:
            return _task_identity_topic(predicate)
        subject = str(getattr(value, "subject", None) or "")
        object_value = str(getattr(value, "object_value", None) or "")
        content = str(getattr(value, "content", None) or "")
        return _task_identity_topic(" ".join(item for item in (subject, object_value, content) if item))

    left, right = topic(candidate), topic(memory)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 4 and longer.endswith(shorter)


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
    raw = str(value or "").strip()
    if raw in {"", "我", "本人", "用户", "user", "me"}:
        return "用户"
    if extractor_type != "llm":
        # 规则抽取可能把第一个 ASCII 实体（例如 RAG）放入 subject；它是任务材料，不是任务所有者。
        return "用户"
    return raw


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
    text_operation, text_attribute = _task_action_and_material(text)

    # 第一人称任务陈述拥有明确所有者；不要让 LLM 在一次输出中把任务材料本身当作 entity，又在下一次输出中只取更短后缀。
    if re.match(r"\s*(?:我|本人|用户)", str(text or "")):
        entity = "用户"
    if "随心记" in text:
        entity = "随心记"

    if any(marker in text for marker in ("大模型", "模型")) and "供应商" in text:
        attribute = "大模型供应商"
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


def semantic_key(entity: str, attribute: str, canonical_topic: str, scope: str = "current") -> str:
    """函数功能：`semantic_key` 负责处理 semantic key，服务于本文件职责：稳定身份和 canonical key。
    传参：
        entity: entity 参数，由调用方传入，类型为 `str`。
        attribute: attribute 参数，由调用方传入，类型为 `str`。
        canonical_topic: canonical topic 参数，由调用方传入，类型为 `str`。
        scope: scope 参数，由调用方传入，类型为 `str`，默认值为 `'current'`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    entity_key = normalize_identity(entity)
    attribute_key = normalize_identity(attribute)
    stable = _STABLE_SEMANTIC_ATTRIBUTES.get(attribute_key)
    if stable is not None:
        return f"semantic:{entity_key}:{normalize_identity(stable)}:{normalize_scope(scope)}"
    digest = hashlib.sha256(normalize_identity(canonical_topic).encode("utf-8")).hexdigest()[:16]
    return f"semantic:{entity_key}:{digest}"


def preference_key(entity: str, topic: str, scope: str = "global") -> str:
    """函数功能：`preference_key` 负责处理 preference key，服务于本文件职责：稳定身份和 canonical key。
    传参：
        entity: entity 参数，由调用方传入，类型为 `str`。
        topic: topic 参数，由调用方传入，类型为 `str`。
        scope: scope 参数，由调用方传入，类型为 `str`，默认值为 `'global'`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return f"preference:{normalize_identity(entity)}:{normalize_identity(topic)}:{normalize_scope(scope)}"


def is_task_lifecycle_statement(text: str) -> bool:
    """函数功能：`is_task_lifecycle_statement` 负责判断是否为 task lifecycle statement，服务于本文件职责：稳定身份和 canonical key。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    raw = str(text or "")
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
        entity, attribute, operation = _task_identity_from_text(source_text, candidate)
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
                "memory_key_version": MEMORY_KEY_V3_VERSION,
            }
        )
        return replace(
            candidate,
            subject=entity,
            predicate=attribute,
            object_value=new_value or candidate.object_value or attribute,
            memory_key=task_key(entity, attribute, operation, canonical_scope),
            scope=scope,
            memory_key_version=MEMORY_KEY_V3_VERSION,
        )

    if memory_type == "preference":
        signature = preference_signature(source_text)
        entity = str(candidate.subject or "用户")
        topic = str(candidate.object_value or signature.topic or source_text)
        canonical_scope = str(scope.get("scope") or (signature.scopes[0] if signature.scopes else "global"))
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
            predicate="preference",
            object_value=topic,
            memory_key=preference_key(entity, topic, canonical_scope),
            scope=scope,
            memory_key_version=MEMORY_KEY_V3_VERSION,
        )

    if memory_type == "semantic":
        entity = str(candidate.subject or "用户")
        attribute = str(candidate.predicate or "fact")
        canonical_topic = str(scope.get("canonical_topic") or candidate.object_value or source_text)
        canonical_scope = str(scope.get("scope") or "current")
        scope.update(
            {
                "canonical_topic": canonical_topic,
                "scope": canonical_scope,
                "memory_key_version": MEMORY_KEY_V3_VERSION,
            }
        )
        return replace(
            candidate,
            subject=entity,
            predicate=attribute,
            memory_key=semantic_key(entity, attribute, canonical_topic, canonical_scope),
            scope=scope,
            memory_key_version=MEMORY_KEY_V3_VERSION,
        )

    scope.update({"memory_key_version": MEMORY_KEY_V3_VERSION})
    return replace(candidate, scope=scope, memory_key_version=MEMORY_KEY_V3_VERSION)
