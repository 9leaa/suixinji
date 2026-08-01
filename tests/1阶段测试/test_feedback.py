"""文件作用：用户反馈存储。

项目关系：本文件依赖 `core`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from core import feedback


def test_save_feedback_writes_jsonl(tmp_path, monkeypatch):
    """函数功能：`test_save_feedback_writes_jsonl` 负责验证 save feedback writes jsonl 场景，服务于本文件职责：用户反馈存储。
    传参：
        tmp_path: tmp path 参数，由调用方传入。
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`test_create_feedback_strips_text` 负责验证 create feedback strips text 场景，服务于本文件职责：用户反馈存储。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    record = feedback.create_feedback_record(
        space_id="space1",
        message_id=None,
        text="  搜不到昨天的任务  ",
    )

    assert record.text == "搜不到昨天的任务"
