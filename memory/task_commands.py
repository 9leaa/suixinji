"""Complete todo inventory, numbered updates and semantic task references."""

from dataclasses import replace
from datetime import datetime
import re
import unicodedata
import uuid

from core.lifecycle import traced
from core import settings
from infrastructure.redis_lock import coordinated_lock
from memory import repository as repo, task_cards, workflow_state as store
from memory.access import memory_access_allowed
from memory.consistency import wait_for_memory_barrier
from memory.models import MemoryDecision, utc_now_iso
from memory.workflow import stable_id

_NUMBER = r"(?:\d+|[一二三四五六七八九十百两]+)"
_REF = rf"(?:(?:第)?{_NUMBER}(?:项|个)?(?:任务)?|任务\s*(?:第)?{_NUMBER}(?:项|个)?)"
_REFS = rf"{_REF}(?:\s*(?:、|,|，|和|与|及|还有)\s*{_REF})*"
_COMPLETE = re.compile(
    rf"^(?:请|帮我|请帮我|麻烦)?\s*(?:把|将)?\s*(?P<refs>{_REFS})\s*(?:这(?:些|两项)任务)?\s*(?:请|都|全部|均|都请)?\s*(?:标记|标为|设为|设置为|改为)(?:为|成)?\s*(?:已)?完成(?:了)?[。.!！]*$"
)
_FINISHED = re.compile(rf"^(?P<refs>{_REFS})\s*(?:都|全部|均)?\s*(?:已经|已)?完成了[。.!！]*$")
_LIST = re.compile(
    r"^(?:请|帮我|请帮我|麻烦)?(?:列出|列举|查看|显示|看看)?(?:一下)?(?:我)?(?:当前|现在|目前)?(?:还)?(?:有)?(?:哪些|什么|所有|全部)?(?:尚未完成|未完成|没完成|没有完成|待办)(?:的)?(?:任务|事项)?(?:有哪些|是什么)?(?:[，,。？?！!\s]*(?:全部|都)?(?:列举|列出|显示)(?:出来|一下)?)*[。？?！!\s]*$"
)


def _number(value):
    if value.isdecimal():
        return int(value)
    digits = dict(zip("一二三四五六七八九", range(1, 10))) | {"两": 2}
    total, current = 0, 0
    for ch in value:
        if ch in digits:
            current = digits[ch]
        else:
            total += (current or 1) * {"十": 10, "百": 100}[ch]
            current = 0
    return total + current


def parse_command(text):
    """Only whole utterances qualify; questions, quotes and negations cannot write."""
    text = unicodedata.normalize("NFKC", text).strip()
    if text.startswith("/ask "):
        text = text[5:].strip()
    if _LIST.fullmatch(text):
        return {"action": "list"}
    match = _COMPLETE.fullmatch(text) or _FINISHED.fullmatch(text)
    if match and "任务" in text:
        return {"action": "complete", "numbers": list(dict.fromkeys(_number(n) for n in re.findall(_NUMBER, match["refs"])))}
    from memory.task_reference import completion_utterance

    if completion_utterance(text):
        return {"action": "complete_named"}
    return None


def _allowed(record, space_id, owner, access_context):
    return (
        record is not None
        and record.space_id == space_id
        and record.memory_type == "task"
        and record.status == "active"
        and record.scope.get("owner_id", "owner") == owner
        and memory_access_allowed(record, access_context or {"requester": owner})
        and (not record.valid_until or datetime.fromisoformat(record.valid_until).astimezone() > datetime.now().astimezone())
    )


def _all_todos(space_id, owner, access_context):
    # records() uses keyset pages until exhaustion, not list_memories' 100-row cap.
    records = [r for r in task_cards.records(space_id, owner) if r.task_status == "todo" and _allowed(r, space_id, owner, access_context)]
    return sorted(records, key=lambda r: (r.created_at, r.id))


def _label(card):
    subject = "" if card["subject"] == "用户" else card["subject"] + "："
    return subject + card["task"] + (f"（{card['instance']}）" if card.get("instance") else "")


def _snapshot(space_id, session, records):
    snapshot = {
        "id": uuid.uuid4().hex,
        "created_at": utc_now_iso(),
        "items": [
            {"number": i, "id": r.id, "version": r.current_version, "label": _label(task_cards.project(r))}
            for i, r in enumerate(records, 1)
        ],
    }
    store.put(space_id, "task_list_snapshot", snapshot["id"], snapshot)
    store.put(space_id, "task_list_latest", session, {"snapshot_id": snapshot["id"]})
    store.put(space_id, "task_reference_context", session, {"ids": [r.id for r in records]})
    return snapshot


def _list(space_id, session, owner, access_context):
    records = _all_todos(space_id, owner, access_context)
    snapshot = _snapshot(space_id, session, records)
    if not records:
        return "当前没有已保存的未完成任务。"
    lines = [f"当前已保存的未完成任务共 {len(records)} 项，以下全部列出："]
    for item, record in zip(snapshot["items"], records):
        description = task_cards.project(record)["description"]
        lines.append(f"任务 {item['number']}：{item['label']}" + (f"\n  {description}" if description else ""))
    lines.append("可以按名称说“合同打印好了”，或回复“任务1、任务5请标记完成”。编号对应本次清单；重新列出后以新清单为准。")
    return "\n\n".join(lines)


def _complete_one(space_id, record, item, operation):
    card = task_cards.project(record)
    text = operation["text"]
    note = {"id": operation["note_id"], "space_id": space_id, "text": text, "ts": operation["created_at"]}
    # Resolution has bound the request to a persisted target; never choose a different card during writing.
    value = task_cards.TaskInput(
        subject=card["subject"],
        task=card["task"],
        instance=card["instance"],
        task_status="done",
        description=f"用户确认：{card['task']}已完成。",
        evidence_span=text,
        intent="update",
    )
    plan = task_cards.TaskPlan(
        action="update",
        target_id=record.id,
        description=value.description,
        subject_relation="same",
        instance_relation="same",
        reason=operation.get("resolution_reason") or "用户明确指定清单编号完成任务",
    )
    task_cards.check_plan(value, plan, [record])
    source = task_cards.source_for(note, value)
    source["task_list_snapshot_id"] = operation["snapshot_id"]
    source["task_number"] = item["number"]
    source["resolution_method"] = "name_or_context" if operation.get("named") else "list_number"
    merged = task_cards.merge(value, record, plan, source)
    candidate = task_cards.candidate_from_row(note["id"], text, value.model_dump())
    scope = {**record.scope, "task_card": merged, "task_input": value.model_dump()}
    scope.pop("closure_reason", None)
    candidate = replace(
        candidate,
        content=task_cards.search_text(merged),
        scope=scope,
        memory_key=record.memory_key,
        memory_key_version=task_cards.VERSION,
        candidate_id="task-command:" + stable_id(operation["note_id"], record.id),
    )
    decision = MemoryDecision(
        candidate_id=candidate.candidate_id,
        decision_id="task-command-decision:" + stable_id(operation["note_id"], record.id),
        relation="update_task",
        recommended_action="update_task",
        target_memory_ids=[record.id],
        target_snapshot_version=record.current_version,
        confidence=1.0,
        reason=plan.reason,
        evidence=["task-list:" + operation["snapshot_id"], "note:" + note["id"]],
    )
    repo.apply_memory_decision(space_id, note["id"], candidate, decision)
    saved = repo.get_memory(record.id)
    if saved.task_status != "done" or task_cards.project(saved)["task_status"] != "done":
        raise ValueError("写入后状态核验失败")


def _complete(space_id, session, owner, command, text, message_id, access_context):
    operation_id = stable_id(session, message_id) if message_id else uuid.uuid4().hex
    operation = store.get(space_id, "task_number_operation", operation_id)
    if operation and operation.get("answer"):
        return operation["answer"]
    if operation is None:
        latest = store.get(space_id, "task_list_latest", session)
        snapshot = store.get(space_id, "task_list_snapshot", latest["snapshot_id"]) if latest else None
        if not snapshot:
            return "请先让我列出当前未完成任务，再按清单编号标记完成。"
        by_number = {i["number"]: i for i in snapshot["items"]}
        invalid = [n for n in command["numbers"] if n not in by_number]
        if invalid:
            return "本次清单中没有任务 " + "、".join(map(str, invalid)) + "；本次未修改任何任务。请重新列出任务核对编号。"
        operation = {
            "snapshot_id": snapshot["id"],
            "text": text,
            "created_at": utc_now_iso(),
            "note_id": "task-command:" + operation_id,
            "items": [by_number[n] for n in command["numbers"]],
        }
        # Persist the selected IDs before any writes; retries must never bind to a newer list.
        store.put(space_id, "task_number_operation", operation_id, operation)
    return _execute_operation(space_id, session, owner, access_context, operation_id, operation)


def _execute_operation(space_id, session, owner, access_context, operation_id, operation):
    selected = [(i, repo.get_memory(i["id"])) for i in operation["items"]]
    stale = [
        i["number"]
        for i, r in selected
        if not _allowed(r, space_id, owner, access_context) or (r.task_status != "done" and r.current_version != i["version"])
    ]
    if stale:
        answer = "任务 " + "、".join(map(str, stale)) + " 已变化或不可操作；本次未修改任何任务。请重新列出任务后再操作。"
    else:
        lines = []
        completed_ids = []
        for item, record in selected:
            try:
                prefix = item["label"] if operation.get("named") else f"任务 {item['number']}：{item['label']}"
                if record.task_status == "done":
                    lines.append(f"{prefix} — 已是 done，无需重复修改。")
                else:
                    _complete_one(space_id, record, item, operation)
                    lines.append(f"{prefix} — 已标记完成（done）。")
                completed_ids.append(record.id)
            except (task_cards.TaskVersionConflict, ValueError) as exc:
                lines.append(f"任务 {item['number']}：未完成修改（{exc}），请重新列出任务核对。")
        if completed_ids:
            store.put(space_id, "task_reference_context", session, {"ids": completed_ids})
        answer = "\n".join(lines)
    operation["answer"] = answer
    store.put(space_id, "task_number_operation", operation_id, operation)
    return answer


@traced("任务清单 · 查询或完成", "memory")
def handle_task_command(space_id, text, *, tenant_id="default", user_id=None, message_id=None, conversation_id=None, access_context=None):
    if not settings.TASK_CARDS_ENABLED:
        return None
    command = parse_command(text)
    if command is None:
        return None
    owner = str(user_id or "owner")
    session = stable_id(tenant_id, owner, conversation_id or space_id)
    if wait_for_memory_barrier(space_id)["status"] == "timeout":
        return "还有消息正在处理，暂时无法确认完整任务清单，请稍后重试。"
    # One list/command at a time per user and conversation. Repository CAS still checks each target version.
    with coordinated_lock("task-list:" + stable_id(space_id, session), critical=True):
        if command["action"] == "list":
            return _list(space_id, session, owner, access_context)
        if command["action"] == "complete_named":
            return _complete_named(space_id, session, owner, text, message_id, access_context, tenant_id)
        return _complete(space_id, session, owner, command, text, message_id, access_context)


def _complete_named(space_id, session, owner, text, message_id, access_context, tenant_id):
    from memory.task_reference import resolve

    operation_id = stable_id(session, message_id) if message_id else uuid.uuid4().hex
    operation = store.get(space_id, "task_number_operation", operation_id)
    if operation:
        if operation.get("answer"):
            return operation["answer"]
        return _execute_operation(space_id, session, owner, access_context, operation_id, operation)
    records = [r for r in task_cards.records(space_id, owner) if _allowed(r, space_id, owner, access_context)]
    context = store.get(space_id, "task_reference_context", session) or {"ids": []}
    try:
        result, audit = resolve(space_id, text, records, context["ids"], user_id=owner, tenant_id=tenant_id)
    except (RuntimeError, ValueError, TypeError) as exc:
        store.put(space_id, "task_reference_resolution", operation_id, {"text": text, "error": str(exc)})
        return "暂时无法准确确认要完成的任务，未修改任何任务。可以先列出未完成任务，再按编号操作。"
    store.put(space_id, "task_reference_resolution", operation_id, audit)
    if result.status == "not_completion":
        return "这条信息没有明确表示整个任务已完成，未修改任务状态。"
    if result.status == "not_found":
        return "没有找到与这句话对应的已有任务，未新建或修改任务。请补充任务名称或先列出未完成任务。"
    by_id = {r.id: r for r in records}
    if result.status == "ambiguous":
        choices = [by_id[key] for key in dict.fromkeys(result.candidate_ids)]
        if not choices:
            return "暂时无法确定是哪项任务，请补充任务名称、执行人或轮次。未修改任务。"
        snapshot = _snapshot(space_id, session, choices)
        return (
            "有多个可能的任务，请确认要完成哪项：\n"
            + "\n".join(f"任务 {i['number']}：{i['label']}" for i in snapshot["items"])
            + "\n可以回复“任务1请标记完成”。本次尚未修改任务。"
        )
    matched = [by_id[key] for key in dict.fromkeys(result.target_ids)]
    # Persist semantic selection separately; never replace a displayed list merely to execute a name.
    snapshot_id = uuid.uuid4().hex
    items = [
        {"number": i, "id": r.id, "version": r.current_version, "label": _label(task_cards.project(r))} for i, r in enumerate(matched, 1)
    ]
    store.put(space_id, "task_list_snapshot", snapshot_id, {"id": snapshot_id, "items": items, "named": True})
    operation = {
        "snapshot_id": snapshot_id,
        "text": text,
        "created_at": utc_now_iso(),
        "note_id": "task-command:" + operation_id,
        "items": items,
        "named": True,
        "resolution_reason": result.reason,
    }
    store.put(space_id, "task_number_operation", operation_id, operation)
    return _execute_operation(space_id, session, owner, access_context, operation_id, operation)
