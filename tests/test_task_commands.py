"""Public query/console path backed by isolated SQLite, not model-generated lists."""

from dataclasses import replace
import re
from unittest.mock import Mock

import pytest
from core import settings
from memory import repository as repo, task_cards, task_commands as commands, workflow_state as store
from memory.models import MemoryDecision
from agent.query_agent import answer_question


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "TASK_CARDS_ENABLED", True)
    monkeypatch.setattr(commands, "wait_for_memory_barrier", lambda *a: {"status": "ready"})
    # Listing and numbered completion must not ask a model to pick or omit IDs.
    monkeypatch.setattr(task_cards, "complete_json", Mock(side_effect=AssertionError("unexpected model call")))


def seed(n, *, owner="owner", space="s", status="todo", extra_scope=None):
    text = f"提交第{n}份测试报告"
    value = task_cards.TaskInput(task=text, task_status=status, description=text, evidence_span=text)
    note = {"id": f"{owner}-{n}", "text": text, "ts": "2026-10-20T09:00:00+08:00"}
    card = task_cards.merge(value, None, task_cards.TaskPlan(action="new"), task_cards.source_for(note, value))
    candidate = task_cards.candidate_from_row(note["id"], text, value.model_dump())
    candidate = replace(
        candidate,
        content=task_cards.search_text(card),
        scope={"owner_id": owner, "access_scope": "owner", "task_card": card, **(extra_scope or {})},
        memory_key=task_cards.card_key("用户", text, None, owner),
    )
    decision = MemoryDecision(
        candidate_id=candidate.candidate_id,
        decision_id=f"{space}-{owner}-{n}",
        relation="new",
        recommended_action="insert",
        confidence=1,
        target_memory_ids=[],
        reason="test fixture",
        evidence=["note:" + note["id"]],
    )
    result = repo.apply_memory_decision(space, note["id"], candidate, decision)
    return repo.get_memory(result["memory_id"])


def latest(space="s"):
    return store.list_items(space, "task_list_snapshot")[-1]


def test_full_inventory_exceeds_both_page_and_old_evidence_limits():
    ids = {seed(n).id for n in range(125)}
    seed(200, status="done")
    seed(201, owner="someone-else")
    answer = answer_question("s", "当前还有哪些未完成任务？列举出来。")
    assert "共 125 项" in answer
    assert len(re.findall(r"^任务 \d+：", answer, re.M)) == 125
    assert {i["id"] for i in latest()["items"]} == ids
    assert all(f"提交第{n}份测试报告" in answer for n in range(125))
    assert "提交第200份" not in answer and "提交第201份" not in answer


def test_complete_original_1_5_versions_sources_and_remaining():
    for n in range(6):
        seed(n)
    answer_question("s", "有哪些待办任务？")
    snapshot = latest()
    selected = [snapshot["items"][i]["id"] for i in (0, 4)]
    result = answer_question("s", "任务1，任务5请标记完成", message_id="complete-1-5")
    assert result.count("已标记完成（done）") == 2
    for item in snapshot["items"]:
        record = repo.get_memory(item["id"])
        assert record.task_status == ("done" if item["id"] in selected else "todo")
        assert record.current_version == (2 if item["id"] in selected else 1)
        if item["id"] in selected:
            card = task_cards.evidence(record)
            assert card["sources"][-1]["original_text"] == "任务1，任务5请标记完成"
            assert card["sources"][-1]["task_number"] == item["number"]
            assert card["sources"][-1]["task_list_snapshot_id"] == snapshot["id"]
    # A second mutation uses the old displayed mapping; completing #1 must not renumber #5.
    again = answer_question("s", "任务5请标记完成", message_id="again")
    assert "已是 done" in again
    remaining = answer_question("s", "列出所有未完成任务")
    assert "共 4 项" in remaining
    # Retried transport request remains bound to its original IDs even after a new list.
    assert answer_question("s", "任务1，任务5请标记完成", message_id="complete-1-5") == result
    assert sum(r.task_status == "done" for r in repo.list_memories("s", limit=100)) == 2


def test_missing_invalid_and_stale_lists_do_not_write():
    record = seed(1)
    assert "先让我列出" in answer_question("s", "任务1请标记完成")
    answer_question("s", "列出未完成任务")
    assert "未修改任何" in answer_question("s", "任务1、任务5请标记完成")
    assert repo.get_memory(record.id).task_status == "todo"
    repo.update_memory(record.id, content="内容已经改变", reason="test concurrent edit")
    assert "已变化" in answer_question("s", "任务1请标记完成")
    assert repo.get_memory(record.id).task_status == "todo"


def test_user_space_and_conversation_snapshots_are_isolated():
    seed(1, owner="alice")
    seed(2, owner="bob")
    seed(3, space="other", owner="alice")
    answer_question("s", "列出未完成任务", user_id="alice", conversation_id="c1")
    assert "先让我列出" in answer_question("s", "任务1请标记完成", user_id="bob", conversation_id="c1")
    assert "先让我列出" in answer_question("s", "任务1请标记完成", user_id="alice", conversation_id="c2")
    assert "先让我列出" in answer_question("other", "任务1请标记完成", user_id="alice", conversation_id="c1")
    answer = answer_question("s", "任务1请标记完成", user_id="alice", conversation_id="c1")
    assert "已标记完成" in answer
    assert next(r for r in repo.list_memories("s", limit=100) if r.scope["owner_id"] == "bob").task_status == "todo"


def test_deleted_snapshot_target_never_rebinds_to_new_task():
    record = seed(1)
    answer_question("s", "列出未完成任务")
    repo.update_memory(record.id, status="forgotten", reason="test forget")
    new = seed(2)
    assert "不可操作" in answer_question("s", "任务1请标记完成")
    assert repo.get_memory(new.id).task_status == "todo"


def test_barrier_does_not_claim_incomplete_list_is_complete(monkeypatch):
    seed(1)
    monkeypatch.setattr(commands, "wait_for_memory_barrier", lambda *a: {"status": "timeout"})
    assert "正在处理" in answer_question("s", "列出未完成任务")
    assert not store.list_items("s", "task_list_snapshot")


@pytest.mark.parametrize(
    "text",
    [
        "任务1，任务5请标记完成",
        "请把任务1和任务5标记为已完成",
        "第1、5项任务标记完成",
        "任务一和任务五已经完成了",
    ],
)
def test_natural_language_completion(text):
    assert commands.parse_command(text) == {"action": "complete", "numbers": [1, 5]}


@pytest.mark.parametrize(
    "text",
    [
        "任务1完成了吗？",
        "不要把任务1标记完成",
        "他说“任务1请标记完成”",
        "如果任务1完成了就标记完成",
        "任务1标记完成，任务5不要改",
        "创建任务1，任务5",
    ],
)
def test_questions_negations_and_mixed_instructions_are_not_writes(text):
    assert commands.parse_command(text) is None


def test_console_natural_utterances_route_to_query_not_ingest():
    from apps.console_commands import route_message

    for text in ["当前还有哪些未完成任务？列举出来。", "任务1，任务5请标记完成"]:
        result = route_message(text)
        assert result["task_type"] == "query" and result["payload"]["consistency"] == "memory"
    assert route_message("明天我要提交报告")["task_type"] == "ingest"


def test_write_conflict_is_reported_and_other_ids_not_rebound(monkeypatch):
    for n in range(2):
        seed(n)
    answer_question("s", "列出未完成任务")
    real = repo.apply_memory_decision

    def conflict(space, note, candidate, decision):
        if "第1份" in candidate.content:
            raise task_cards.TaskVersionConflict("task changed")
        return real(space, note, candidate, decision)

    monkeypatch.setattr(repo, "apply_memory_decision", conflict)
    result = answer_question("s", "任务1、任务2请标记完成")
    assert "未完成修改" in result and result.count("已标记完成") == 1


def test_long_delivery_preserves_all_text_and_numbers(monkeypatch):
    from bot import feishu_bot
    from core.text_chunks import text_chunks

    text = "\n".join(f"任务 {n}：请提交研发报告😀" for n in range(501))
    parts = text_chunks(text)
    assert len(parts) > 1 and "".join(parts) == text
    assert all(len(p.encode("utf-8")) <= 6000 for p in parts)
    send = Mock()
    monkeypatch.setattr(feishu_bot, "_send_text_part", send)
    feishu_bot.send_text("fake-chat", text)
    assert "".join(c.args[1] for c in send.call_args_list) == text


def test_structured_query_entry_uses_complete_inventory():
    from agent.query_agent import answer_question_result

    for n in range(15):
        seed(n)
    result = answer_question_result("s", "列出未完成任务")
    assert result.reason_code == "task_command" and "共 15 项" in result.answer


def test_local_worker_forwards_task_owner(monkeypatch):
    from core import worker

    monkeypatch.setattr(settings, "PREFERENCE_CARDS_ENABLED", False)
    monkeypatch.setattr(worker, "load_index", lambda *a: [])
    write = Mock(return_value={})
    monkeypatch.setattr(worker, "process_note_memory", write)
    from memory import runtime_workflow

    monkeypatch.setattr(runtime_workflow, "flush_notifications", lambda *a: None)
    worker._process_memory_with_route({"id": "n", "space_id": "s", "text": "提交报告"}, {"sender": {"open_id": "alice"}})
    assert write.call_args.args[0]["user_id"] == "alice"


def test_console_handler_executes_both_steps_with_original_user(monkeypatch):
    from apps import handlers

    seed(1, owner="alice")
    seed(2, owner="alice")
    seed(3, owner="bob")
    monkeypatch.setattr(handlers, "FAKE_EXTERNALS", False)
    monkeypatch.setattr(settings, "PREFERENCE_CARDS_ENABLED", False)
    replies = []
    monkeypatch.setattr(handlers, "_enqueue_delivery", lambda task, **kw: replies.append(kw["text"]))
    for i, text in enumerate(["列出未完成任务", "任务1、任务2请标记完成"]):
        handlers.handle_query(
            {
                "id": f"t{i}",
                "space_id": "s",
                "tenant_id": "tenant",
                "source_message_id": f"m{i}",
                "payload_json": {"question": text, "user_id": "alice", "reply_channel": "console", "chat_id": f"console:{i}"},
            }
        )
    assert "共 2 项" in replies[0]
    assert replies[1].count("已标记完成（done）") == 2
    assert next(r for r in repo.list_memories("s", limit=100) if r.scope["owner_id"] == "bob").task_status == "todo"


def test_sensitive_and_expired_tasks_are_not_listed():
    seed(1, extra_scope={"sensitivity": "sensitive"})
    expired = seed(2)
    repo.update_memory(expired.id, valid_until="2020-01-01T00:00:00+00:00")
    assert answer_question("s", "列出未完成任务") == "当前没有已保存的未完成任务。"


@pytest.mark.parametrize("text", ["当前还有哪些未完成任务？列举出来。", "任务1，任务5请标记完成"])
def test_feishu_natural_language_uses_query_and_carries_owner(monkeypatch, text):
    import json
    from types import SimpleNamespace
    from bot import feishu_bot
    from repositories.postgres.dispatch import DispatchResult

    sender = SimpleNamespace(
        sender_id=SimpleNamespace(open_id="ou-alice", user_id="alice", union_id="un-alice"), sender_type="user", tenant_key="t"
    )
    message = SimpleNamespace(message_type="text", chat_id="chat-a", chat_type="p2p", message_id="msg", content=json.dumps({"text": text}))
    event = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender), header=SimpleNamespace(event_id="event"))
    calls = []
    monkeypatch.setattr(feishu_bot, "TASK_QUEUE_BACKEND", "redis_streams")
    monkeypatch.setattr(feishu_bot, "safe_send_text", Mock(return_value=True))
    monkeypatch.setattr(feishu_bot, "receive", lambda command: calls.append(command) or DispatchResult("i", "task", True, False))
    feishu_bot.handle_text_message(event)
    assert len(calls) == 1 and calls[0].task_type == "query"
    assert calls[0].task_payload["user_id"] == "alice"
    assert calls[0].task_payload["consistency"] == "memory"
    assert calls[0].task_payload["question"] == text
    assert calls[0].text == text
