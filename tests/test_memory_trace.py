from memory.trace import add_step, find_traces_by_memory, finish_trace, get_trace, latest_trace, start_trace


def test_trace_round_trip_and_memory_lookup():
    trace = start_trace("memory_write", "space-1", note_id="note-1")
    add_step(trace, "memory_inserted", output_summary={"memory_id": "mem-1"})
    finished = finish_trace(trace)

    assert finished is not None
    assert latest_trace()["trace_id"] == trace["trace_id"]
    assert get_trace(trace["trace_id"])["trace_type"] == "memory_write"
    assert find_traces_by_memory("mem-1")[0]["trace_id"] == trace["trace_id"]
    assert all("duration_ms" in step for step in latest_trace()["steps"])
