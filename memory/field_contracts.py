"""Shared Layer-1 field contracts and deterministic normalization helpers."""
from __future__ import annotations
import re
from typing import Any

MEMORY_TYPES = ("task", "semantic", "preference", "episodic")
TASK_STATUSES = ("todo", "blocked", "done", "cancelled")
SEMANTIC_ATTRIBUTE_ALIASES = {
    "location": "location", "居住地": "location", "住址": "location", "所在地": "location",
    "current_project": "current_project", "currentproject": "current_project", "当前项目": "current_project", "项目": "current_project",
    "current_employer": "current_employer", "currentemployer": "current_employer", "当前雇主": "current_employer", "工作单位": "current_employer", "公司": "current_employer",
    "learning_focus": "learning_focus", "learningfocus": "learning_focus", "学习重点": "learning_focus", "学习方向": "learning_focus",
    "birthplace": "birthplace", "籍贯": "birthplace", "出生地": "birthplace",
    "school": "school", "学校": "school", "当前学校": "school",
    "major": "major", "专业": "major", "job_target": "job_target", "求职目标": "job_target", "目标岗位": "job_target",
    "primary_device": "primary_device", "常用设备": "primary_device", "主要设备": "primary_device",
    "preferred_language": "preferred_language", "偏好交流语言": "preferred_language", "交流语言": "preferred_language",
}
SEMANTIC_CANONICAL_TOPICS = {
    "location": "用户当前居住地", "current_project": "用户当前项目", "current_employer": "用户当前雇主",
    "learning_focus": "用户当前学习重点", "birthplace": "用户籍贯", "school": "用户当前学校",
    "major": "用户当前专业", "job_target": "用户求职目标", "primary_device": "用户常用设备",
    "preferred_language": "用户偏好交流语言",
}
TASK_OPERATION_ALIASES = {
    "学习": "学习", "学": "学习", "学完": "学习", "换": "更换", "换成": "更换", "更换": "更换",
    "更换为": "更换", "替换": "更换", "替换成": "更换", "改成": "修改", "修改": "修改",
    "制作": "制作", "做": "制作", "做完": "制作", "开发": "开发", "实现": "实现", "修复": "修复",
    "完善": "完善", "部署": "部署", "发布": "发布", "迁移": "迁移", "整理": "整理", "测试": "测试",
    "审查": "审查", "评测": "评测", "执行": "执行", "完成": "执行",
}
TASK_STATUS_ALIASES = {
    "todo": "todo", "pending": "todo", "in_progress": "todo", "in progress": "todo", "进行中": "todo",
    "正在": "todo", "继续": "todo", "blocked": "blocked", "阻塞": "blocked", "卡住": "blocked",
    "等待权限": "blocked", "等待数据": "blocked", "done": "done", "completed": "done", "complete": "done",
    "完成": "done", "已完成": "done", "做完": "done", "搞定": "done", "cancelled": "cancelled",
    "canceled": "cancelled", "取消": "cancelled", "不做了": "cancelled", "不用做": "cancelled",
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
        ("cancelled", ("取消", "不做了", "不用做", "不再维护")),
        ("blocked", ("阻塞", "卡住", "等待权限", "等待数据", "等确认")),
        ("done", ("已完成", "完成", "做完", "搞定", "弄好", "发布成功", "学完")),
        ("todo", ("正在", "进行中", "继续", "需要", "计划", "准备", "待办", "要做")),
    ):
        if any(marker in text for marker in markers):
            return status
    return None

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
    if value:
        direct = SEMANTIC_ATTRIBUTE_ALIASES.get(value)
        if direct:
            return direct
        value = value.casefold().replace(" ", "_")
        if value in SEMANTIC_CANONICAL_TOPICS:
            return value
    text = str(source_text or "")
    for markers, attr in (
        (("住在", "居住", "搬到", "住址"), "location"),
        (("项目", "开发", "负责"), "current_project"),
        (("工作", "雇主", "跳槽"), "current_employer"),
        (("学习", "研究", "重点"), "learning_focus"),
        (("籍贯", "出生地"), "birthplace"),
        (("学校", "大学"), "school"),
        (("专业",), "major"),
        (("求职", "岗位", "目标职位"), "job_target"),
        (("设备", "电脑", "手机"), "primary_device"),
        (("语言",), "preferred_language"),
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
    text = re.sub(r"(?:正在|已经|开始|继续|需要|计划|准备|记得|完成|做完|了)$", "", text)
    return text.strip(" ：:，,。！？!?") or None

def preference_topic(source_text: str, topic_hint: Any = None, value_hint: Any = None) -> str | None:
    try:
        from memory.policies.preference import preference_signature
        topic = preference_signature(source_text).topic
        if topic:
            return clean_text(topic)
    except Exception:
        pass
    return clean_text(value_hint) or clean_text(topic_hint)

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
