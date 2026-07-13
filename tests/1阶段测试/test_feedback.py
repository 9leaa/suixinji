from core import feedback


def test_save_feedback_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", tmp_path)

    record = feedback.save_feedback(
        space_id="g/test space",
        message_id="msg-1",
        text="这次总结漏了健身计划",
    )

    assert record.status == "open"
    assert record.text == "这次总结漏了健身计划"

    items = feedback.list_feedback("g/test space")
    assert len(items) == 1
    assert items[0]["id"] == record.id
    assert items[0]["message_id"] == "msg-1"
    assert items[0]["text"] == "这次总结漏了健身计划"


def test_create_feedback_strips_text():
    record = feedback.create_feedback_record(
        space_id="space1",
        message_id=None,
        text="  搜不到昨天的任务  ",
    )

    assert record.text == "搜不到昨天的任务"
