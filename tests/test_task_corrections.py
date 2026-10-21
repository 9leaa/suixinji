"""Explicit edits preserve task IDs, ownership, versions and search identity."""

from unittest.mock import Mock
import pytest
from pydantic import ValidationError
from core import settings
from memory import extractor, service, repository as repo, task_cards as cards, task_retrieval
from memory.task_schema import TaskInput


@pytest.fixture
def write(monkeypatch):
    monkeypatch.setattr(settings, "TASK_CARDS_ENABLED", True)
    for module in (extractor, service):
        monkeypatch.setattr(module, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(task_retrieval, "vector_rank", lambda *a: [])
    notes = []

    def apply(text, *, task="购买团队车票", subject="用户", status="todo", intent="auto", evidence=None, changes=None, target=None, **kw):
        correction_fields = kw.pop(
            "correction_fields", list(changes) if changes else (["task"] if task != "购买团队车票" else ["task_status"])
        )
        data = TaskInput(
            correction_fields=correction_fields if intent == "correct" else [],
            task=task,
            subject=subject,
            task_status=status,
            description=text,
            evidence_span=text,
            intent=intent,
            change_evidence=evidence,
            **kw,
        )
        monkeypatch.setattr(extractor, "complete_json", Mock(return_value={"candidates": [data.model_dump()]}))
        if target:
            monkeypatch.setattr(
                cards,
                "complete_json",
                Mock(
                    return_value={
                        "action": "update",
                        "target_id": target,
                        "description": text,
                        "reason": "当前消息明确更新同一任务",
                        "subject_relation": "different" if subject != "用户" else "same",
                        "instance_relation": "same",
                        "changes": changes or {},
                    }
                ),
            )
        note = {
            "id": f"note-{len(notes)}",
            "space_id": "s",
            "user_id": "alice",
            "text": text,
            "ts": f"2026-10-20T09:{len(notes):02d}:00+08:00",
            "previous_messages": list(reversed(notes)),
        }
        result = service.process_note_memory(note)
        notes.append({"note_id": note["id"], "text": text, "ts": note["ts"], "role": "user"})
        return result

    return apply


def only():
    return next(cards.records("s", "alice"))


def test_transfer_updates_database_identity_not_ownership(write):
    write("我要购买团队车票")
    before = only()
    text = "团队车票购买改由哥哥负责，任务继续"
    write(text, subject="哥哥", intent="transfer", evidence=text, changes={"subject": "哥哥"}, target=before.id)
    after = only()
    assert after.id == before.id and after.subject == "哥哥" and after.task_status == "todo"
    assert after.scope["owner_id"] == "alice" and after.scope["access_scope"] == before.scope["access_scope"]
    assert after.scope.get("subject_id") is None
    assert after.memory_key != before.memory_key
    assert after.scope["task_family_key"] == cards.family_key("哥哥", "购买团队车票", "alice")
    assert after.current_version == 2
    assert cards.project(after)["sources"][-1]["field_changes"]["subject"] == {"before": "用户", "after": "哥哥"}
    assert not list(cards.records("s", "哥哥"))
    assert any("用户" in v.content for v in after.versions)


def test_correct_room_updates_search_title_and_keeps_due(write):
    write("周五预约301会议室", task="预约301会议室", due_expression="周五")
    before = only()
    text = "纠正房间号，是预约302会议室"
    write(text, task="预约302会议室", intent="correct", evidence=text, changes={"task": "预约302会议室"}, target=before.id)
    after = only()
    card = cards.project(after)
    assert card["task"] == after.object_value == after.scope["canonical_topic"] == "预约302会议室"
    assert card["due_at"] == "2026-10-23"
    assert "301" not in after.content and "302" in after.content
    assert any(
        r["id"] == after.id
        for r in service.memory_search("s", "302", memory_type="task", min_score=0, access_context={"requester": "alice"})
    )


def test_correct_false_done(write):
    write("团队车票已经购买完成", status="done")
    before = only()
    text = "纠正上一条，完成是口误，车票其实还没买"
    write(text, intent="correct", evidence=text, target=before.id)
    assert only().id == before.id and only().task_status == "todo"


@pytest.mark.parametrize("text", ["如果转交哥哥负责就好了", "不要转交哥哥负责", "是否转交哥哥负责？", "哥哥也要买车票"])
def test_transfer_label_is_not_authorization(write, text):
    write("我要购买团队车票")
    before = only()
    write(text, intent="transfer", subject="哥哥", evidence=text, changes={"subject": "哥哥"}, target=before.id)
    assert only().current_version == before.current_version and only().subject == "用户"


def test_update_cannot_smuggle_identity_patch(write):
    write("我要购买团队车票")
    before = only()
    write("哥哥也买车票", subject="哥哥", changes={"subject": "哥哥"}, target=before.id)
    assert only().current_version == before.current_version


def test_transfer_cannot_reopen_done(write):
    write("车票买完了", status="done")
    before = only()
    text = "车票任务转交哥哥负责"
    write(text, intent="transfer", subject="哥哥", evidence=text, changes={"subject": "哥哥"}, target=before.id)
    assert only().task_status == "done" and only().subject == "用户"


def test_correction_requires_patch_for_changed_title(write):
    write("我要预约301", task="预约301")
    before = only()
    text = "纠正，是预约302"
    write(text, task="预约302", intent="correct", evidence=text, target=before.id)
    assert only().object_value == "预约301"


def test_cross_round_still_rejected(write):
    write("本周购买团队车票", period_expression="本周")
    before = only()
    text = "纠正，下周的车票改由哥哥负责"
    write(text, subject="哥哥", period_expression="下周", intent="correct", evidence=text, changes={"subject": "哥哥"}, target=before.id)
    assert only().current_version == before.current_version


def test_schema_disallows_owner_patch():
    with pytest.raises(ValidationError):
        cards.TaskChanges.model_validate({"owner_id": "哥哥"})


def test_correct_completion_uses_ingestion_route():
    from memory.task_commands import parse_command

    assert parse_command("纠正上一条，采购申请已经完成了") is None


def test_cannot_trim_condition_from_evidence(write):
    write("我要购买团队车票")
    before = only()
    write(
        "如果转交哥哥负责就好了", intent="transfer", subject="哥哥", evidence="转交哥哥负责", changes={"subject": "哥哥"}, target=before.id
    )
    assert only().current_version == before.current_version


def test_correct_other_field_does_not_reopen_done(write):
    write("预约301完成", task="预约301", status="done")
    before = only()
    text = "纠正，是预约302"
    write(text, task="预约302", intent="correct", evidence=text, changes={"task": "预约302"}, target=before.id)
    assert only().task_status == "done" and only().object_value == "预约301"


def test_source_reference_cannot_update_another_card(write, monkeypatch):
    write("我要购买团队车票")
    first = only()
    write("我要领取样品", task="领取样品", intent="new")
    records = list(cards.records("s", "alice"))
    second = next(r for r in records if r.id != first.id)
    text = "团队车票转交哥哥负责"
    write(
        text,
        intent="transfer",
        subject="哥哥",
        evidence=text,
        changes={"subject": "哥哥"},
        target=second.id,
        antecedent_note_id="note-0",
        antecedent_evidence_span="我要购买团队车票",
    )
    assert repo.get_memory(first.id).subject == repo.get_memory(second.id).subject == "用户"


def test_transfer_preserves_restricted_scope(write):
    write("我要购买团队车票")
    before = only()
    scope = {**before.scope, "access_scope": "private"}
    import json
    from memory import workflow_state

    with workflow_state.transaction("s") as state:
        state.execute("UPDATE memories SET scope_json=:scope WHERE id=:id", {"scope": json.dumps(scope), "id": before.id})
    text = "购买团队车票转交哥哥负责"
    write(text, intent="transfer", subject="哥哥", evidence=text, changes={"subject": "哥哥"}, target=before.id)
    assert only().scope["access_scope"] == "private"


def test_changed_identity_reembeds_cache(monkeypatch):
    from types import SimpleNamespace
    from memory.task_retrieval import vector_rank

    model = Mock(return_value=[1.0, 0.0])
    monkeypatch.setattr(task_retrieval, "embed_text", model)
    record = SimpleNamespace(id="same-id")
    card = {"subject": "用户", "task": "预约301", "instance": None}
    value = TaskInput(task="预约302", task_status="todo", evidence_span="预约302")
    vector_rank("cache", value, [(record, card)])
    assert model.call_count == 2
    card["task"] = "预约302"
    vector_rank("cache", value, [(record, card)])
    assert model.call_count == 4
    vector_rank("cache", value, [(record, card)])
    assert model.call_count == 5


def test_correct_status_keeps_synonymous_title(write):
    write("上传合同完成", task="上传合同", status="done")
    before = only()
    text = "纠正，上传完成是口误，合同还未上传"
    write(text, task="上传合同文件", intent="correct", evidence=text, correction_fields=["task_status"], target=before.id)
    assert only().task_status == "todo" and only().object_value == "上传合同"


def test_unchanged_deadline_copied_from_cited_source_is_not_recalculated(write):
    write("我要预约401会议室，截止周五下午3点", task="预约401会议室", due_expression="周五下午3点")
    before = only()
    text = "纠正，是预约402会议室，截止时间不变"
    write(
        text,
        task="预约402会议室",
        intent="correct",
        evidence=text,
        changes={"task": "预约402会议室"},
        target=before.id,
        due_expression="周五下午3点",
        antecedent_note_id="note-0",
        antecedent_evidence_span="我要预约401会议室，截止周五下午3点",
    )
    assert only().object_value == "预约402会议室"
    assert cards.project(only())["due_at"] == cards.project(before)["due_at"]


def test_correction_rejects_unrequested_subject_patch(write):
    write("我要购买团队车票")
    before = only()
    text = "纠正，车票还没买"
    write(
        text,
        intent="correct",
        evidence=text,
        subject="哥哥",
        changes={"subject": "哥哥"},
        correction_fields=["task_status"],
        target=before.id,
    )
    assert only().subject == "用户"


def test_patch_whitespace_does_not_block_correction(write):
    write("预约301会议室", task="预约301会议室")
    before = only()
    text = "纠正，是预约 302 会议室"
    write(text, task="预约 302 会议室", intent="correct", evidence=text, changes={"task": "预约302会议室"}, target=before.id)
    assert only().object_value == "预约302会议室"


def test_room_correction_cannot_accidentally_complete_task(write):
    write("预约301会议室", task="预约301会议室")
    before = only()
    text = "纠正，是预约302会议室"
    write(
        text,
        task="预约302会议室",
        status="done",
        intent="correct",
        evidence=text,
        changes={"task": "预约302会议室"},
        correction_fields=["task"],
        target=before.id,
    )
    assert only().task_status == "todo" and only().object_value == "预约301会议室"
