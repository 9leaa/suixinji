"""Shared Layer-1 field contracts and deterministic normalization helpers."""
from __future__ import annotations
import re
from typing import Any

from memory.models import normalize_content

MEMORY_TYPES = ("task", "semantic", "preference", "episodic")
TASK_STATUSES = ("todo", "done")
# Semantic attributes are deliberately broad facets. A semantic assertion is
# append-only, so this field is for grouping/retrieval rather than state-slot
# identity. Keep this list static until the offline taxonomy workflow exists.
SEMANTIC_ATTRIBUTES = (
    "identity", "location", "education", "career", "project",
    "learning", "capability", "device", "other",
)
SEMANTIC_ATTRIBUTE_ALIASES = {
    "identity": "identity", "身份": "identity", "个人信息": "identity", "交流语言": "identity", "preferred_language": "identity", "偏好交流语言": "identity",
    "location": "location", "居住地": "location", "住址": "location", "所在地": "location", "出生地": "location", "籍贯": "location", "birthplace": "location",
    "education": "education", "教育": "education", "学校": "education", "大学": "education", "专业": "education", "school": "education", "major": "education",
    "career": "career", "职业": "career", "工作": "career", "雇主": "career", "公司": "career", "工作单位": "career", "求职目标": "career", "目标岗位": "career", "current_employer": "career", "job_target": "career",
    "project": "project", "项目": "project", "当前项目": "project", "current_project": "project",
    "learning": "learning", "学习": "learning", "学习重点": "learning", "学习方向": "learning", "研究": "learning", "learning_focus": "learning",
    "capability": "capability", "能力": "capability", "技能": "capability", "技术栈": "capability",
    "device": "device", "设备": "device", "电脑": "device", "手机": "device", "常用设备": "device", "主要设备": "device", "primary_device": "device",
    "other": "other", "其它": "other", "其他": "other", "fact": "other", "事实": "other",
}
SEMANTIC_CANONICAL_TOPICS = {
    "identity": "身份相关事实", "location": "地点相关事实", "education": "教育相关事实",
    "career": "职业相关事实", "project": "项目相关事实", "learning": "学习相关事实",
    "capability": "能力相关事实", "device": "设备相关事实", "other": "其他长期事实",
}
TASK_OPERATION_ALIASES = {
    "学习": "学习", "学": "学习", "学完": "学习", "换": "更换", "换成": "更换", "更换": "更换",
    "更换为": "更换", "替换": "更换", "替换成": "更换", "改成": "修改", "修改": "修改",
    "制作": "制作", "做": "制作", "做完": "制作", "开发": "开发", "实现": "实现", "修复": "修复", "提交": "提交", "上线": "上线",
    "完善": "完善", "部署": "部署", "发布": "发布", "迁移": "迁移", "整理": "整理", "测试": "测试",
    "审查": "审查", "评测": "评测", "执行": "执行", "完成": "执行",
    "处理": "处理", "维护": "维护",
}
TASK_STATUS_ALIASES = {
    "todo": "todo", "pending": "todo", "in_progress": "todo", "in progress": "todo", "进行中": "todo",
    "正在": "todo", "继续": "todo", "blocked": "todo", "阻塞": "todo", "卡住": "todo",
    "等待权限": "todo", "等待数据": "todo", "done": "done", "completed": "done", "complete": "done",
    "完成": "done", "已完成": "done", "做完": "done", "搞定": "done", "cancelled": "done",
    "canceled": "done", "取消": "done", "不做了": "done", "不用做": "done", "abandoned": "done", "放弃": "done",
}

TASK_CLOSURE_ALIASES = {
    "cancelled": "cancelled", "canceled": "cancelled", "取消": "cancelled", "不做了": "cancelled", "不用做": "cancelled",
    "abandoned": "abandoned", "放弃": "abandoned",
    "completed": "completed", "complete": "completed", "done": "completed", "完成": "completed", "做完": "completed", "搞定": "completed",
}

def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None

def normalize_entity(value: Any, *, memory_type: str | None = None) -> str | None:
    value = clean_text(value)
    if value in {None, "", "我", "本人", "用户", "user", "me"}:
        return "用户" if memory_type in MEMORY_TYPES else value
    return value

def normalize_task_status(value: Any, source_text: str = "") -> str | None:
    value = clean_text(value)
    if value and value.lower() in TASK_STATUS_ALIASES:
        return TASK_STATUS_ALIASES[value.lower()]
    if value and value in TASK_STATUS_ALIASES:
        return TASK_STATUS_ALIASES[value]
    text = str(source_text or "")
    for status, markers in (
        ("done", ("取消", "不做了", "不用做", "不再维护", "放弃")),
        ("todo", ("阻塞", "卡住", "等待权限", "等待数据", "等确认")),
        ("done", ("已完成", "完成", "做完", "搞定", "弄好", "发布成功", "已提交", "提交", "上线", "学完")),
        ("todo", ("正在", "进行中", "继续", "需要", "计划", "准备", "待办", "要做")),
    ):
        if any(marker in text for marker in markers):
            return status
    return None


def project_legacy_task_scope(raw_status: Any, scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project legacy task status at read time without writing it back.

    New writes remain restricted to ``todo``/``done``.  For old rows, callers
    get a normalized status plus a non-sensitive diagnostic/semantic marker so
    downstream code can distinguish a blocker, in-progress note, or closure.
    """
    projected = dict(scope or {})
    raw = str(raw_status or "").strip().casefold()
    if raw not in {"blocked", "in_progress", "cancelled", "canceled"}:
        return projected
    projected["legacy_task_status"] = raw
    if raw == "blocked":
        projected.setdefault("blocker", "legacy_blocked")
    elif raw == "in_progress":
        projected.setdefault("progress_note", "legacy_in_progress")
    else:
        projected.setdefault("closure_reason", "cancelled")
    return projected


def task_closure_reason(value: Any = None, source_text: str = "") -> str | None:
    """Return why a task ended without expanding the two-state status enum."""
    cleaned = clean_text(value)
    if cleaned:
        direct = TASK_CLOSURE_ALIASES.get(cleaned.casefold()) or TASK_CLOSURE_ALIASES.get(cleaned)
        if direct:
            return direct
    text = str(source_text or "")
    if any(marker in text for marker in ("取消", "不做了", "不用做", "不再维护")):
        return "cancelled"
    if "放弃" in text:
        return "abandoned"
    if any(marker in text for marker in ("已完成", "完成", "做完", "搞定", "弄好", "发布成功", "已提交", "已经提交", "提交", "上线", "学完")):
        return "completed"
    return None


def task_progress_metadata(source_text: str) -> dict[str, str]:
    """Preserve progress/blocking semantics as metadata, never as task state."""
    text = str(source_text or "").strip().strip("。！？!?；;")
    metadata: dict[str, str] = {}
    # Store the semantic detail, not the whole sentence containing the task
    # identity.  This keeps blocker/progress fields useful to consolidation
    # and makes them stable across equivalent wording.
    blocker = re.search(r"(?:被|因|由于|卡在)\s*([^，,。！？!?；;]+?)\s*(?:卡住|阻塞|拦住|挡住)(?:了)?$", text)
    if blocker:
        value = blocker.group(1).strip(" ：:，,。！？!?；;")
        # “被权限阻塞” is conventionally surfaced as waiting for permission;
        # an explicit resource problem (“被接口文档不完整卡住”) remains the
        # resource text itself.
        metadata["blocker"] = f"等待{value}" if value in {"权限", "数据", "管理员权限"} else value
    elif "暂停" in text:
        metadata["blocker"] = "暂停"
    elif re.search(r"(?:等待|等)\s*[^，,。！？!?；;]+", text):
        waiting = re.search(r"((?:等待|等)\s*[^，,。！？!?；;]+)", text)
        if waiting:
            phrase = waiting.group(1).strip(" ：:，,。！？!?；;")
            metadata["blocker"] = phrase if phrase.startswith("等待") else f"等待{phrase[1:]}"
    elif any(marker in text for marker in ("阻塞", "卡住", "等待权限", "等待数据", "等确认")):
        # A bare blocker still has a bounded phrase after the task status
        # marker; avoid leaking unrelated task text into the field.
        tail = re.split(r"(?:现在|目前|当前|正在|已经|已)", text, maxsplit=1)[-1]
        metadata["blocker"] = tail.replace("卡住", "").replace("阻塞", "").strip(" ：:，,。！？!?；;") or text
    else:
        progress = re.search(r"((?:继续处理|正在修复失败用例|正在补充测试|正在处理|进行中|暂停))", text)
        if progress:
            metadata["progress_note"] = progress.group(1).strip(" ：:，,。！？!?；;")
    return metadata

def normalize_operation(value: Any, source_text: str = "") -> str | None:
    value = clean_text(value)
    if value in {"完成", "完成了", "已完成", "做完", "已做完"}:
        value = None
    if value:
        for alias, canonical in sorted(TASK_OPERATION_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
            if alias in value or canonical in value:
                return canonical
        return value
    text = str(source_text or "")
    for alias, canonical in sorted(TASK_OPERATION_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in text and alias not in {"完成", "做完"}:
            return canonical
    return "执行" if "完成" in text else None

def normalize_semantic_attribute(value: Any, source_text: str = "") -> str | None:
    value = clean_text(value)
    # fact and other are placeholders, not evidence that a fact belongs to the
    # broad other facet. Infer a concrete facet from grounded source first.
    generic_hint = value in {"fact", "事实", "other", "其它", "其他"}
    if value and not generic_hint:
        direct = SEMANTIC_ATTRIBUTE_ALIASES.get(value)
        if direct:
            return direct
        value = value.casefold().replace(" ", "_")
        if value in SEMANTIC_ATTRIBUTES:
            return value
    text = str(source_text or "")
    for markers, attr in (
        (("住在", "居住", "搬到", "住址"), "location"),
        (("籍贯", "出生地"), "location"),
        (("学校", "大学", "专业"), "education"),
        (("求职", "岗位", "目标职位", "工作", "雇主", "跳槽", "公司"), "career"),
        (("项目", "开发", "负责"), "project"),
        (("学习", "研究", "重点"), "learning"),
        (("设备", "电脑", "手机"), "device"),
        (("技能", "能力", "擅长", "会用"), "capability"),
        (("语言", "身份", "姓名"), "identity"),
    ):
        if any(marker in text for marker in markers):
            return attr
    return None

def semantic_topic(attribute: str | None, fallback: Any = None) -> str | None:
    return SEMANTIC_CANONICAL_TOPICS.get(attribute) or clean_text(fallback)

def task_attribute(value: Any, source_text: str = "") -> str | None:
    value = clean_text(value)
    if value and value not in {"task", "任务", "待办", "当前任务", "事项", "fact"}:
        value = re.sub(r"^(?:正在|继续|已经|需要|计划|准备|待办|记得)", "", value).strip(" ：:，,。！？!?")
        if value:
            return value
    text = re.sub(r"\s+", "", str(source_text or ""))
    text = re.sub(r"^(?:我|本人|用户)(?:需要|准备|计划|正在|继续|记得|请)", "", text)
    text = re.sub(r"(?:正在|已经|开始|继续|需要|计划|准备|记得|完成|完成了|做完|已做完|了)$", "", text)
    return text.strip(" ：:，,。！？!?") or None

_GENERIC_PREFERENCE_TOPICS = {
    "偏好",
    "用户偏好",
    "未指定偏好主题",
    "东西",
    "这个",
    "它",
}


def preference_topic_with_source(
    source_text: str,
    topic_hint: Any = None,
    value_hint: Any = None,
) -> tuple[str | None, str | None]:
    """Choose a preference topic and identify whether it came from LLM/rules."""
    llm_topic = clean_text(value_hint) or clean_text(topic_hint)
    normalized_source = normalize_content(source_text)
    rule_topic = None
    try:
        from memory.policies.preference import preference_signature

        rule_topic = clean_text(preference_signature(source_text).topic)
    except Exception:
        pass
    llm_canonical_topic = None
    try:
        from memory.policies.preference import preference_signature

        llm_canonical_topic = clean_text(preference_signature(llm_topic or "").topic)
    except Exception:
        pass
    llm_supported = bool(
        llm_topic
        and llm_topic not in _GENERIC_PREFERENCE_TOPICS
        and normalize_content(llm_topic) in normalized_source
    )
    # A rule-derived specific topic is the stable identity.  The LLM may
    # phrase it with modifiers ("无糖咖啡"), which are represented separately
    # as qualifiers rather than making a second Topic/key.
    if rule_topic:
        if llm_supported and llm_canonical_topic and normalize_content(llm_canonical_topic) == normalize_content(rule_topic):
            return rule_topic, "llm_validated"
        return rule_topic, "rules"
    if llm_supported and llm_canonical_topic:
        return llm_canonical_topic, "llm"
    return llm_canonical_topic or llm_topic, None


def preference_topic(source_text: str, topic_hint: Any = None, value_hint: Any = None) -> str | None:
    return preference_topic_with_source(source_text, topic_hint, value_hint)[0]


def preference_scope_with_source(source_text: str, scope_hint: Any = None) -> tuple[str | None, str | None]:
    """Resolve an explicit preference scope and retain its provenance."""
    hint = clean_text(scope_hint)
    normalized_source = normalize_content(source_text)
    if hint and hint.casefold() not in {"global", "当前", "current"} and normalize_content(hint) in normalized_source:
        return hint, "llm"
    try:
        from memory.policies.preference import preference_signature
        scopes = preference_signature(source_text).scopes
        if scopes:
            return clean_text(scopes[0]), "rules"
    except Exception:
        pass
    return None, None

def episodic_topic(source_text: str, topic_hint: Any = None, value_hint: Any = None) -> str | None:
    text = str(source_text or "").strip()
    value = clean_text(value_hint) or clean_text(topic_hint)
    if value and value in text:
        return value
    text = re.sub(r"^(?:今天|昨天|前天|刚才|上周|本周|上午|下午|晚上|最近)", "", text)
    text = re.sub(r"^(?:我|我们|用户)", "", text)
    return clean_text(text.strip(" ：:，,。！？!?"))

def canonical_topic_for(memory_type: str, *, source_text: str, entity: Any = None, attribute: Any = None,
                        operation: Any = None, topic_hint: Any = None, new_value: Any = None) -> str | None:
    if memory_type == "preference":
        return preference_topic(source_text, topic_hint, new_value)
    if memory_type == "semantic":
        return semantic_topic(normalize_semantic_attribute(attribute, source_text), topic_hint)
    if memory_type == "task":
        entity = normalize_entity(entity, memory_type="task") or "用户"
        attribute = task_attribute(attribute, source_text) or "任务"
        operation = normalize_operation(operation, source_text) or "维护"
        return f"{operation}{'' if entity == '用户' else entity}{attribute}"
    return episodic_topic(source_text, topic_hint, new_value)

def contract_for(memory_type: str) -> dict[str, Any]:
    return {
        "preference": {"attribute": "preference", "operation": None, "task_status": None},
        "semantic": {"attribute": "enum", "operation": None, "task_status": None},
        "episodic": {"attribute": "event", "operation": None, "task_status": None},
        "task": {"attribute": "required", "operation": "required", "task_status": "required"},
    }.get(memory_type, {})
