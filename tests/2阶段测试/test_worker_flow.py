from types import SimpleNamespace

from core import worker


RECORD = {
    "id": "record-1",
    "message_id": "message-1",
    "space_id": "space-1",
    "ts": "2026-06-07T10:00:00+08:00",
    "text": "记得测试 P4 自动总结。",
}


def test_process_record_saves_provisional_note_without_waiting_for_llm(monkeypatch):
    saved_notes = []
    marked = []

    monkeypatch.setattr(worker, "note_exists", lambda space_id, message_id: False)
    monkeypatch.setattr(
        worker,
        "classify_text_local",
        lambda text: SimpleNamespace(
            title="测试 P4 自动总结",
            tags=["待办", "提醒"],
            type="任务",
            summary="需要测试 P4 自动总结。",
        ),
    )
    monkeypatch.setattr(worker, "classify_text", lambda text: (_ for _ in ()).throw(AssertionError("slow classifier must run in enrichment")))
    monkeypatch.setattr(worker, "embed_text", lambda text: (_ for _ in ()).throw(AssertionError("embedding must run in enrichment")))
    monkeypatch.setattr(worker, "save_note", lambda note: saved_notes.append(note))
    monkeypatch.setattr(worker, "process_note_memory", lambda note, classification=None: None)
    monkeypatch.setattr(worker, "mark_processed", lambda space_id, record_id: marked.append((space_id, record_id)))

    worker.process_record(dict(RECORD))

    assert len(saved_notes) == 1
    note = saved_notes[0]
    assert note.id == "record-1"
    assert note.message_id == "message-1"
    assert note.title == "测试 P4 自动总结"
    assert note.type == "任务"
    assert note.tags == ["待办", "提醒"]
    assert note.related == []
    assert note.enrichment_status == "provisional"
    assert note.enrichment_attempts == 0

    assert marked == [("space-1", "record-1")]


def test_process_record_existing_note_marks_processed_without_slow_backfill(monkeypatch):
    calls = []
    existing_note = {"id": "record-1", "message_id": "message-1", "space_id": "space-1", "text": "正文"}

    monkeypatch.setattr(worker, "note_exists", lambda space_id, message_id: True)
    monkeypatch.setattr(worker, "_find_note_by_message_id", lambda space_id, message_id: existing_note)
    monkeypatch.setattr(worker, "get_extraction_state", lambda note_id: SimpleNamespace(status="completed"))
    monkeypatch.setattr(worker, "backfill_vector_if_missing", lambda *args: (_ for _ in ()).throw(AssertionError("must not backfill inline")))
    monkeypatch.setattr(worker, "mark_processed", lambda space_id, record_id: calls.append(("marked", space_id, record_id)))
    monkeypatch.setattr(worker, "classify_text", lambda text: (_ for _ in ()).throw(AssertionError("should not classify existing note")))

    worker.process_record(dict(RECORD))

    assert calls == [("marked", "space-1", "record-1")]


def test_process_record_existing_note_recovers_retryable_memory_once(monkeypatch):
    calls = []
    existing_note = {"id": "note-1", "message_id": "message-1", "space_id": "space-1", "text": "我喜欢咖啡"}

    monkeypatch.setattr(worker, "note_exists", lambda space_id, message_id: True)
    monkeypatch.setattr(worker, "_find_note_by_message_id", lambda space_id, message_id: existing_note)
    monkeypatch.setattr(worker, "get_extraction_state", lambda note_id: SimpleNamespace(status="failed"))
    monkeypatch.setattr(worker, "process_note_memory", lambda note: calls.append(("memory", note["id"])))
    monkeypatch.setattr(worker, "mark_processed", lambda space_id, record_id: calls.append(("marked", space_id, record_id)))
    monkeypatch.setattr(worker, "classify_text", lambda text: (_ for _ in ()).throw(AssertionError("should not classify existing note")))

    worker.process_record(dict(RECORD))

    assert calls == [
        ("memory", "note-1"),
        ("marked", "space-1", "record-1"),
    ]


def test_backfill_vector_if_missing_writes_vector_from_existing_note(monkeypatch):
    added = []
    notes = [
        {
            "id": "note-1",
            "message_id": "message-1",
            "text": "已有笔记正文",
            "title": "已有笔记",
            "tags": ["资料", "备查"],
            "type": "资料",
            "summary": "已有笔记摘要。",
            "ts": "2026-06-07T10:00:00+08:00",
        }
    ]

    monkeypatch.setattr(worker, "load_index", lambda space_id: notes)
    monkeypatch.setattr(worker, "vector_item_exists", lambda space_id, note_id, message_id: False)
    monkeypatch.setattr(worker, "embed_text", lambda text: [1.0, 0.0])
    monkeypatch.setattr(worker, "add_vector_item", lambda space_id, item: added.append((space_id, item)) or True)

    assert worker.backfill_vector_if_missing("space-1", "message-1") is True

    assert len(added) == 1
    space_id, item = added[0]
    assert space_id == "space-1"
    assert item.note_id == "note-1"
    assert item.message_id == "message-1"
    assert item.text == "已有笔记正文"
    assert item.embedding == [1.0, 0.0]
    assert item.metadata["summary"] == "已有笔记摘要。"


def test_backfill_vector_if_missing_skips_existing_vector(monkeypatch):
    notes = [{"id": "note-1", "message_id": "message-1", "text": "正文"}]

    monkeypatch.setattr(worker, "load_index", lambda space_id: notes)
    monkeypatch.setattr(worker, "vector_item_exists", lambda space_id, note_id, message_id: True)
    monkeypatch.setattr(worker, "embed_text", lambda text: (_ for _ in ()).throw(AssertionError("should not embed existing vector")))

    assert worker.backfill_vector_if_missing("space-1", "message-1") is False


def test_enrich_note_runs_slow_work_and_marks_ready(monkeypatch):
    updates = []
    vectors = []
    note = {
        "id": "note-1",
        "message_id": "message-1",
        "space_id": "space-1",
        "ts": "2026-06-07T10:00:00+08:00",
        "text": "我喜欢喝乌龙茶",
        "enrichment_status": "provisional",
        "enrichment_attempts": 0,
        "sensitivity": "normal",
    }
    classification = SimpleNamespace(title="喜欢乌龙茶", tags=["饮食", "日常"], type="生活", summary="用户喜欢乌龙茶。")

    monkeypatch.setattr(worker, "find_note", lambda space_id, note_id: dict(note))
    monkeypatch.setattr(worker, "vector_item_exists", lambda *args: False)
    monkeypatch.setattr(worker, "update_note_metadata", lambda space_id, note_id, **kwargs: updates.append(kwargs) or {**note, **kwargs})
    monkeypatch.setattr(worker, "classify_text", lambda text: classification)
    monkeypatch.setattr(worker, "embed_text", lambda text: [1.0, 0.0])
    monkeypatch.setattr(worker, "search_related_note_ids", lambda *args, **kwargs: ["older-note"])
    monkeypatch.setattr(worker, "add_vector_item", lambda space_id, item: vectors.append(item) or True)

    assert worker.enrich_note("space-1", "note-1") is True
    assert updates[0]["enrichment_status"] == "enriching"
    assert updates[-1]["enrichment_status"] == "ready"
    assert updates[-1]["related"] == ["older-note"]
    assert vectors[0].text == "我喜欢喝乌龙茶"


def test_process_record_redacts_sensitive_pending_wal_before_storage(monkeypatch):
    blocked = []
    record = {**RECORD, "text": "密码是Abcd1234"}
    monkeypatch.setattr(worker, "mark_sensitive_blocked", lambda space_id, record_id, category: blocked.append((space_id, record_id, category)))
    monkeypatch.setattr(worker, "save_note", lambda note: (_ for _ in ()).throw(AssertionError("must not save")))

    assert worker.process_record(record) is None
    assert blocked == [("space-1", "record-1", "credential")]


def test_process_pending_skips_duplicate_message_ids(monkeypatch):
    records = [
        {"id": "r1", "message_id": "m1", "space_id": "space-1"},
        {"id": "r2", "message_id": "m1", "space_id": "space-1"},
        {"id": "r3", "message_id": "m2", "space_id": "space-1"},
    ]
    processed = []
    marked = []

    monkeypatch.setattr(worker, "load_pending_records", lambda space_id: records)
    monkeypatch.setattr(worker, "process_record", lambda record: processed.append(record["id"]))
    monkeypatch.setattr(worker, "mark_processed", lambda space_id, record_id: marked.append((space_id, record_id)))

    count = worker.process_pending("space-1")

    assert count == 3
    assert processed == ["r1", "r3"]
    assert marked == [("space-1", "r2")]
