"""文件作用：Trace 写入、隐私裁剪、latest 展示。

项目关系：本文件依赖 `memory.repository`、`memory.trace`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from memory.trace import add_step, find_traces_by_memory, finish_trace, get_trace, latest_trace, start_trace
from memory.repository import _connect


def test_trace_round_trip_and_memory_lookup():
    """函数功能：`test_trace_round_trip_and_memory_lookup` 负责验证 trace round trip and memory lookup 场景，服务于本文件职责：Trace 写入、隐私裁剪、latest 展示。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    trace = start_trace("memory_write", "space-1", note_id="note-1")
    add_step(trace, "memory_inserted", output_summary={"memory_id": "mem-1"})
    finished = finish_trace(trace)

    assert finished is not None
    assert latest_trace()["trace_id"] == trace["trace_id"]
    assert get_trace(trace["trace_id"])["trace_type"] == "memory_write"
    assert find_traces_by_memory("mem-1")[0]["trace_id"] == trace["trace_id"]
    assert all("duration_ms" in step for step in latest_trace()["steps"])
    with _connect() as conn:
        stored = conn.execute("SELECT payload_json FROM memory_traces WHERE trace_id = ?", (trace["trace_id"],)).fetchone()
    assert stored is not None


def test_trace_redacts_llm_previews_and_secret_values():
    """函数功能：`test_trace_redacts_llm_previews_and_secret_values` 负责验证 trace redacts llm previews and secret values 场景，服务于本文件职责：Trace 写入、隐私裁剪、latest 展示。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    trace = start_trace("memory_write", "space-1", note_id="note-1")
    add_step(trace, "memory_write_failed", status="failed", error="text_preview='密码: abc' token=secret-value")
    finish_trace(trace, status="failed")

    error = latest_trace()["steps"][0]["error"]
    assert "密码: abc" not in error
    assert "secret-value" not in error
