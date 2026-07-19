from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bot import feishu_bot
from repositories.postgres.dispatch import DispatchResult


def _message_event(text: str, message_id: str = "message-dup") -> SimpleNamespace:
    sender_id = SimpleNamespace(open_id="open-test", user_id="user-test", union_id="union-test")
    sender = SimpleNamespace(sender_id=sender_id, sender_type="user", tenant_key="tenant-test")
    message = SimpleNamespace(
        message_type="text",
        chat_id="chat-test",
        chat_type="p2p",
        message_id=message_id,
        content=json.dumps({"text": text}, ensure_ascii=False),
        mentions=[],
    )
    return SimpleNamespace(
        header=SimpleNamespace(event_id=f"event-{message_id}"),
        event=SimpleNamespace(sender=sender, message=message),
    )


def _patch_duplicate_runtime(monkeypatch, result: DispatchResult) -> tuple[list[str], list[tuple[str, dict]]]:
    sent: list[str] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(feishu_bot, "TASK_QUEUE_BACKEND", "redis_streams")
    monkeypatch.setattr(feishu_bot, "safe_send_text", lambda _chat_id, text: sent.append(text) or True)
    monkeypatch.setattr(feishu_bot, "receive", lambda _command: result)
    monkeypatch.setattr(feishu_bot, "log_event", lambda action, **kwargs: events.append((action, kwargs)))
    return sent, events


@pytest.mark.parametrize(
    ("text", "task_type"),
    [
        ("今天记一笔", "ingest"),
        ("/ask 上次说的咖啡店在哪", "query"),
        ("/summary 今天", "summary"),
    ],
)
def test_duplicate_feishu_stream_events_are_silent(monkeypatch, text: str, task_type: str) -> None:
    sent, events = _patch_duplicate_runtime(
        monkeypatch,
        DispatchResult("inbox-dup", None, False, True),
    )

    feishu_bot.handle_text_message(_message_event(text))

    assert sent == []
    duplicate_events = [item for item in events if item[0] == "feishu.message.duplicate"]
    assert duplicate_events
    assert duplicate_events[-1][1]["extra"]["task_type"] == task_type


def test_in_progress_feishu_stream_events_are_silent(monkeypatch) -> None:
    sent, events = _patch_duplicate_runtime(
        monkeypatch,
        DispatchResult("inbox-processing", None, False, False, True),
    )

    feishu_bot.handle_text_message(_message_event("/ask 现在有什么待办"))

    assert sent == []
    duplicate_events = [item for item in events if item[0] == "feishu.message.duplicate"]
    assert duplicate_events[-1][1]["status"] == "in_progress"


def test_first_ask_event_receives_before_visible_reply(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(feishu_bot, "TASK_QUEUE_BACKEND", "redis_streams")
    monkeypatch.setattr(feishu_bot, "log_event", lambda *_args, **_kwargs: None)

    def receive(_command):
        order.append("receive")
        return DispatchResult("inbox-first", "task-first", True, False)

    def send(_chat_id: str, text: str) -> bool:
        order.append(f"send:{text}")
        return True

    monkeypatch.setattr(feishu_bot, "receive", receive)
    monkeypatch.setattr(feishu_bot, "safe_send_text", send)

    feishu_bot.handle_text_message(_message_event("/ask 上次说的咖啡店在哪", "message-first"))

    assert order == ["receive", "send:我去翻一下随心记。"]


def test_local_duplicate_ingest_event_is_silent(monkeypatch) -> None:
    sent: list[str] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(feishu_bot, "TASK_QUEUE_BACKEND", "local")
    monkeypatch.setattr(feishu_bot, "safe_send_text", lambda _chat_id, text: sent.append(text) or True)
    monkeypatch.setattr(feishu_bot, "log_event", lambda action, **kwargs: events.append((action, kwargs)))
    monkeypatch.setattr(feishu_bot, "create_pending_record", lambda **kwargs: SimpleNamespace(id="record-local", **kwargs))
    monkeypatch.setattr(feishu_bot, "append_message_once", lambda _record: False)

    feishu_bot.handle_text_message(_message_event("今天记一笔", "message-local"))

    assert sent == []
    assert any(action == "feishu.message.duplicate" for action, _kwargs in events)
