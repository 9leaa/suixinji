"""文件作用：Stage 4 负载测试场景。

项目关系：本文件依赖 `runtime`、`runtime.load_testing`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from runtime import load_testing
from runtime.load_testing import SubmissionResult, execute_load, generate_requests, summarize_plan


def test_multi_user_generator_has_isolated_spaces_and_expected_mix():
    """函数功能：`test_multi_user_generator_has_isolated_spaces_and_expected_mix` 负责验证 multi user generator has isolated spaces and expected mix 场景，服务于本文件职责：Stage 4 负载测试场景。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    requests = generate_requests(users=100, messages_per_user=10, run_id="stage4-test", seed=42)
    plan = summarize_plan(requests)
    assert plan["submitted"] == 1000
    assert plan["unique_users"] == 100
    assert plan["unique_spaces"] == 100
    assert 600 <= plan["operations"]["ingest"] <= 800
    assert plan["operations"]["summary"] > 0
    assert plan["operations"]["memory"] > 0
    assert all(item.space_id.endswith(item.user_id.rsplit("-", 1)[-1]) for item in requests)
    malicious = [item for item in requests if item.user_profile == "malicious"]
    assert len({item.message_id for item in malicious}) < len(malicious)


def test_execute_load_reports_submission_conservation(monkeypatch):
    """函数功能：`test_execute_load_reports_submission_conservation` 负责验证 execute load reports submission conservation 场景，服务于本文件职责：Stage 4 负载测试场景。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    requests = generate_requests(users=2, messages_per_user=2, run_id="stage4-submit")
    outcomes = iter([
        SubmissionResult("accepted", 10, 200),
        SubmissionResult("duplicate", 11, 200),
        SubmissionResult("rate_limited", 12, 429),
        SubmissionResult("failed", 13, error="offline"),
    ])
    monkeypatch.setattr(load_testing, "submit_request", lambda *_args, **_kwargs: next(outcomes))
    report = execute_load(requests, endpoint="http://receiver", concurrency=1)
    assert report["accepted"] == 1
    assert report["duplicate"] == 1
    assert report["rate_limited"] == 1
    assert report["failed"] == 1
    assert report["submission_conservation_ok"]
    assert report["p95_submission_latency_ms"] == 13
