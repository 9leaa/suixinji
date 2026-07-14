from types import SimpleNamespace

from core import worker


RECORD = {
    "id": "record-1",
    "message_id": "message-1",
    "space_id": "space-1",
    "ts": "2026-06-07T10:00:00+08:00",
    "text": "记得测试 P4 自动总结。",
}


def test_process_record_saves_new_note_vector_and_marks_processed(monkeypatch):
    saved_notes = []
    vector_items = []
    marked = []

    monkeypatch.setattr(worker, "note_exists", lambda space_id, message_id: False)
    monkeypatch.setattr(
        worker,
        "classify_text",
        lambda text: SimpleNamespace(
            title="测试 P4 自动总结",
            tags=["待办", "提醒"],
            type="任务",
            summary="需要测试 P4 自动总结。",
        ),
    )
    monkeypatch.setattr(worker, "embed_text", lambda text: [0.1, 0.2, 0.3])

    def fake_search(space_id, embedding, *, top_k, exclude_note_id, min_score):
        assert space_id == "space-1"
        assert embedding == [0.1, 0.2, 0.3]
        assert top_k == 3
        assert exclude_note_id == "record-1"
        assert min_score == 0.5
        return ["old-note-1"]

    monkeypatch.setattr(worker, "search_related_note_ids", fake_search)
    monkeypatch.setattr(worker, "save_note", lambda note: saved_notes.append(note))
    monkeypatch.setattr(worker, "add_vector_item", lambda space_id, item: vector_items.append((space_id, item)) or True)
    monkeypatch.setattr(worker, "mark_processed", lambda space_id, record_id: marked.append((space_id, record_id)))

    worker.process_record(dict(RECORD))

    assert len(saved_notes) == 1
    note = saved_notes[0]
    assert note.id == "record-1"
    assert note.message_id == "message-1"
    assert note.title == "测试 P4 自动总结"
    assert note.type == "任务"
    assert note.tags == ["待办", "提醒"]
    assert note.related == ["old-note-1"]

    assert len(vector_items) == 1
    vector_space_id, vector_item = vector_items[0]
    assert vector_space_id == "space-1"
    assert vector_item.note_id == "record-1"
    assert vector_item.message_id == "message-1"
    assert vector_item.embedding == [0.1, 0.2, 0.3]
    assert vector_item.metadata["title"] == "测试 P4 自动总结"
    assert vector_item.metadata["tags"] == ["待办", "提醒"]

    assert marked == [("space-1", "record-1")]


def test_process_record_existing_note_backfills_vector_and_marks_processed(monkeypatch):
    calls = []

    monkeypatch.setattr(worker, "note_exists", lambda space_id, message_id: True)
    monkeypatch.setattr(worker, "backfill_vector_if_missing", lambda space_id, message_id: calls.append((space_id, message_id)) or True)
    monkeypatch.setattr(worker, "mark_processed", lambda space_id, record_id: calls.append(("marked", space_id, record_id)))
    monkeypatch.setattr(worker, "classify_text", lambda text: (_ for _ in ()).throw(AssertionError("should not classify existing note")))

    worker.process_record(dict(RECORD))

    assert calls == [
        ("space-1", "message-1"),
        ("marked", "space-1", "record-1"),
    ]


def test_process_record_existing_note_recovers_retryable_memory_once(monkeypatch):
    calls = []
    existing_note = {"id": "note-1", "message_id": "message-1", "space_id": "space-1", "text": "我喜欢咖啡"}

    monkeypatch.setattr(worker, "note_exists", lambda space_id, message_id: True)
    monkeypatch.setattr(worker, "_find_note_by_message_id", lambda space_id, message_id: existing_note)
    monkeypatch.setattr(worker, "backfill_vector_if_missing", lambda space_id, message_id: calls.append(("vector", space_id, message_id)) or False)
    monkeypatch.setattr(worker, "get_extraction_state", lambda note_id: SimpleNamespace(status="failed"))
    monkeypatch.setattr(worker, "process_note_memory", lambda note: calls.append(("memory", note["id"])))
    monkeypatch.setattr(worker, "mark_processed", lambda space_id, record_id: calls.append(("marked", space_id, record_id)))
    monkeypatch.setattr(worker, "classify_text", lambda text: (_ for _ in ()).throw(AssertionError("should not classify existing note")))

    worker.process_record(dict(RECORD))

    assert calls == [
        ("vector", "space-1", "message-1"),
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
