"""文件作用：裁决/合并异常的安全失败。

项目关系：本文件依赖 `memory`、`memory.consolidator`、`memory.repository`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from memory import service
from memory.consolidator import process_unextracted_notes
from memory.repository import mark_extraction_completed, mark_extraction_failed


def test_process_unextracted_notes_isolates_bad_note(monkeypatch):
    """函数功能：`test_process_unextracted_notes_isolates_bad_note` 负责验证 process unextracted notes isolates bad note 场景，服务于本文件职责：裁决/合并异常的安全失败。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    notes = [
        {"id": "note-a", "space_id": "space-1", "text": "a"},
        {"id": "note-b", "space_id": "space-1", "text": "b"},
        {"id": "note-c", "space_id": "space-1", "text": "c"},
    ]
    calls = []

    def fake_process(note):
        """函数功能：`fake_process` 负责处理 fake，服务于本文件职责：裁决/合并异常的安全失败。
        传参：
            note: note 参数，由调用方传入。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        calls.append(note["id"])
        if note["id"] == "note-b":
            mark_extraction_failed(note["id"], "space-1", error="bad note")
            raise RuntimeError("bad note")
        mark_extraction_completed(note["id"], "space-1", candidate_count=1, processed_count=1)
        return {"trace_id": f"trace-{note['id']}", "candidates": 1, "extraction_status": "completed"}

    monkeypatch.setattr("memory.consolidator.load_index", lambda space_id: notes)
    monkeypatch.setattr(service, "process_note_memory", fake_process)

    report = process_unextracted_notes("space-1")

    assert calls == ["note-a", "note-b", "note-c"]
    assert report["processed_count"] == 2
    assert report["failed_count"] == 1
    assert report["status"] == "partial"
    assert report["failed"][0]["note_id"] == "note-b"


def test_process_unextracted_notes_next_round_retries_only_failed_note(monkeypatch):
    """函数功能：`test_process_unextracted_notes_next_round_retries_only_failed_note` 负责验证 process unextracted notes next round retries only failed note 场景，服务于本文件职责：裁决/合并异常的安全失败。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    notes = [
        {"id": "note-a", "space_id": "space-1", "text": "a"},
        {"id": "note-b", "space_id": "space-1", "text": "b"},
        {"id": "note-c", "space_id": "space-1", "text": "c"},
    ]
    first_calls = []
    second_calls = []

    def first_process(note):
        """函数功能：`first_process` 负责处理 first，服务于本文件职责：裁决/合并异常的安全失败。
        传参：
            note: note 参数，由调用方传入。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        first_calls.append(note["id"])
        if note["id"] == "note-b":
            mark_extraction_failed(note["id"], "space-1", error="bad note")
            raise RuntimeError("bad note")
        mark_extraction_completed(note["id"], "space-1", candidate_count=1, processed_count=1)
        return {"trace_id": f"trace-{note['id']}", "candidates": 1, "extraction_status": "completed"}

    def second_process(note):
        """函数功能：`second_process` 负责处理 second，服务于本文件职责：裁决/合并异常的安全失败。
        传参：
            note: note 参数，由调用方传入。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        second_calls.append(note["id"])
        mark_extraction_completed(note["id"], "space-1", candidate_count=1, processed_count=1)
        return {"trace_id": f"trace-{note['id']}", "candidates": 1, "extraction_status": "completed"}

    monkeypatch.setattr("memory.consolidator.load_index", lambda space_id: notes)
    monkeypatch.setattr(service, "process_note_memory", first_process)
    first = process_unextracted_notes("space-1")
    monkeypatch.setattr(service, "process_note_memory", second_process)
    second = process_unextracted_notes("space-1")

    assert first["status"] == "partial"
    assert first_calls == ["note-a", "note-b", "note-c"]
    assert second_calls == ["note-b"]
    assert second["status"] == "completed"
    assert second["processed_count"] == 1
    assert second["skipped_count"] == 2


def test_process_unextracted_notes_does_not_swallow_unexpected_state_checks(monkeypatch):
    """函数功能：`test_process_unextracted_notes_does_not_swallow_unexpected_state_checks` 负责验证 process unextracted notes does not swallow unexpected state checks 场景，服务于本文件职责：裁决/合并异常的安全失败。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr("memory.consolidator.load_index", lambda space_id: [{"id": "", "space_id": "space-1"}])

    report = process_unextracted_notes("space-1")

    assert report["processed_count"] == 0
    assert report["failed_count"] == 0
    assert report["skipped_count"] == 1
    assert report["status"] == "completed"
