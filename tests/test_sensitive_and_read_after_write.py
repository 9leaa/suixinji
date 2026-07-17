import json
from types import SimpleNamespace

from agent import query_agent
from bot import feishu_bot
from core.sensitive import assess_sensitive_text, safe_text_preview


def test_sensitive_detector_is_general_not_value_specific():
    samples = [
        "密码是Abcd1234",
        "API_KEY: sk-examplevalue123456",
        "Authorization: Bearer abcdefghijklmnop",
        "银行卡号 6222 0212 3456 7890",
        "wss://example.invalid/ws?access_key=temporary-value&ticket=temporary-ticket",
    ]
    assert all(assess_sensitive_text(sample).blocks_storage for sample in samples)
    assert assess_sensitive_text("我喜欢喝乌龙茶").blocks_storage is False
    assert "Abcd1234" not in safe_text_preview(samples[0])


def test_sensitive_query_returns_locally_without_model_or_embedding(monkeypatch):
    monkeypatch.setattr(query_agent, "complete_json", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not call model")))
    monkeypatch.setattr(query_agent, "embed_text", lambda text: (_ for _ in ()).throw(AssertionError("must not embed")))
    monkeypatch.setattr(query_agent, "memory_search", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not search")))

    answer = query_agent.answer_question("space-1", "我以前保存的密码是什么？")

    assert "不会保存或检索" in answer


def test_query_reads_provisional_note_without_waiting_for_model(monkeypatch):
    notes = [
        {
            "id": "note-new",
            "message_id": "message-new",
            "space_id": "space-1",
            "ts": "2026-07-15T21:00:00+08:00",
            "title": "我喜欢喝乌龙茶",
            "type": "生活",
            "tags": ["饮食", "日常"],
            "summary": "我喜欢喝乌龙茶",
            "text": "我喜欢喝乌龙茶",
            "related": [],
            "enrichment_status": "enriching",
            "sensitivity": "normal",
        }
    ]
    monkeypatch.setattr(query_agent, "load_index", lambda space_id: notes)
    monkeypatch.setattr(query_agent, "complete_json", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not wait for model")))
    monkeypatch.setattr(query_agent, "embed_text", lambda text: (_ for _ in ()).throw(AssertionError("must not embed")))

    answer = query_agent.answer_question("space-1", "我喜欢喝什么？")

    assert "乌龙茶" in answer
    assert "后台完善分类" in answer
    assert "note:note-new" in answer


def test_legacy_sensitive_note_is_filtered_from_deterministic_queries(monkeypatch):
    notes = [
        {
            "id": "secret-note",
            "ts": "2026-07-15T21:00:00+08:00",
            "title": "账号",
            "type": "资料",
            "tags": ["备查"],
            "summary": "凭据",
            "text": "密码是Abcd1234",
            "related": [],
        }
    ]
    monkeypatch.setattr(query_agent, "load_index", lambda space_id: notes)

    assert query_agent.filter_notes("space-1", note_type="资料") == []
    assert query_agent.get_note("space-1", "secret-note")["error"].startswith("note not found")


def test_feishu_ingress_blocks_secret_before_command_or_wal_raw_text(monkeypatch):
    records = []
    replies = []
    message = SimpleNamespace(
        message_type="text",
        chat_id="chat-1",
        chat_type="p2p",
        message_id="message-1",
        content=json.dumps({"text": "/ask 密码是Abcd1234"}, ensure_ascii=False),
        mentions=[],
    )
    sender_id = SimpleNamespace(open_id="open-1", user_id=None, union_id=None)
    sender = SimpleNamespace(sender_type="user", tenant_key="tenant", sender_id=sender_id)
    data = SimpleNamespace(
        header=SimpleNamespace(event_id="event-1"),
        event=SimpleNamespace(message=message, sender=sender),
    )
    monkeypatch.setattr(feishu_bot, "append_message_once", lambda record: records.append(record) or True)
    monkeypatch.setattr(feishu_bot, "safe_send_text", lambda chat_id, text: replies.append(text) or True)
    monkeypatch.setattr(feishu_bot, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(feishu_bot, "get_task_executor", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not queue command")))

    feishu_bot.handle_text_message(data)

    assert records[0].status == "blocked_sensitive"
    assert records[0].text == "[敏感内容已拦截，原文未保存]"
    assert "Abcd1234" not in records[0].text
    assert "未发送给模型" in replies[0]
