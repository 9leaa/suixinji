from __future__ import annotations

from runtime.distributed_metrics import build_report, percentile


def test_percentile_uses_measured_values():
    assert percentile([], 0.95) is None
    assert percentile([1, 2, 3, 100], 0.50) == 3
    assert percentile([1, 2, 3, 100], 0.95) == 100


def test_distributed_report_enforces_conservation():
    database = {
        "accepted": 7,
        "root_task_status": {"completed": 5, "queued": 1, "dead_letter": 1},
        "retry_count": 3,
        "p95_latency_ms": 250,
        "p95_queue_wait_ms": 40,
        "p95_execution_ms": 200,
        "llm_tokens": 123,
        "estimated_cost": 0.02,
    }
    streams = {"stream_lag": 2, "stream_pending": 1}
    submission = {"submitted": 11, "duplicate": 2, "rate_limited": 1, "failed": 1}
    report = build_report(database, streams, submission=submission, locks={"p95_lock_wait_ms": 8})
    assert report["completed"] == 5
    assert report["pending"] == 1
    assert report["failed"] == 2
    assert report["conservation_ok"]
    assert report["conservation_delta"] == 0
    assert report["p95_lock_wait_ms"] == 8
