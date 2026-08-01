"""文件作用：候选 schema、安全、质量、拒绝原因。

项目关系：本文件依赖 `memory.candidate_validator`、`memory.models`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from memory.candidate_validator import contains_sensitive_data, validate_candidate
from memory.models import MemoryCandidate


def test_candidate_validator_rejects_sensitive_note_even_when_candidate_looks_safe():
    """函数功能：`test_candidate_validator_rejects_sensitive_note_even_when_candidate_looks_safe` 负责验证 candidate validator rejects sensitive note even when candidate looks safe 场景，服务于本文件职责：候选 schema、安全、质量、拒绝原因。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    candidate = MemoryCandidate("semantic", "用户正在开发项目", 0.8, 0.9)

    checked, rejection = validate_candidate(candidate, note_text="项目密码: abc123456")

    assert checked is None
    assert rejection.reason == "sensitive_data"


def test_candidate_validator_clamps_scores_and_deduplicates_entities():
    """函数功能：`test_candidate_validator_clamps_scores_and_deduplicates_entities` 负责验证 candidate validator clamps scores and deduplicates entities 场景，服务于本文件职责：候选 schema、安全、质量、拒绝原因。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    candidate = MemoryCandidate(
        "semantic",
        "用户正在开发随心记项目",
        3.0,
        2.0,
        entities=["Agent", "Agent", "RAG"],
        evidence_span="开发随心记项目",
    )

    checked, rejection = validate_candidate(candidate, note_text="我正在开发随心记项目")

    assert rejection is None
    assert checked.confidence == 1.0
    assert checked.importance == 1.0
    assert checked.entities == ["Agent", "RAG"]


def test_sensitive_pattern_detection_handles_long_financial_numbers():
    """函数功能：`test_sensitive_pattern_detection_handles_long_financial_numbers` 负责验证 sensitive pattern detection handles long financial numbers 场景，服务于本文件职责：候选 schema、安全、质量、拒绝原因。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    assert contains_sensitive_data("账号 6222021234567890123") is True
