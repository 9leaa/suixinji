"""文件作用：Layer3 Stage 2 向量 seed 与诊断测试。

项目关系：本文件依赖 `eval.layer3.run_layer3_eval`；被 暂无静态导入方或仅作为入口脚本执行。
"""

from __future__ import annotations

from eval.layer3 import run_layer3_eval


def test_embed_query_for_diagnostic_reports_contract_and_dimension(monkeypatch):
    """函数功能：`test_embed_query_for_diagnostic_reports_contract_and_dimension` 负责验证 embed query for diagnostic reports contract and dimension 场景，服务于本文件职责：Layer3 Stage 2 向量 seed 与诊断测试。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(run_layer3_eval, "_embedding_contract_dict", lambda: {"model": "mock-model", "dimension": 3, "embedding_version": "v1"})
    monkeypatch.setattr("core.llm_client.embed_text", lambda _query: [0.1, 0.2, 0.3])

    embedding, status = run_layer3_eval._embed_query_for_diagnostic("hello")

    assert embedding == [0.1, 0.2, 0.3]
    assert status["status"] == "success"
    assert status["dimension"] == 3
    assert status["contract"]["model"] == "mock-model"


def test_complete_seed_memory_vectors_promotes_claimed_vectors(monkeypatch):
    """函数功能：`test_complete_seed_memory_vectors_promotes_claimed_vectors` 负责验证 complete seed memory vectors promotes claimed vectors 场景，服务于本文件职责：Layer3 Stage 2 向量 seed 与诊断测试。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    claims = {
        "m1": {"text": "alpha", "content_hash": "hash-1", "model": "mock-model", "dimension": 3, "embedding_version": "v1"},
        "m2": {"text": "beta", "content_hash": "hash-2", "model": "mock-model", "dimension": 3, "embedding_version": "v1"},
    }
    completed = []

    monkeypatch.setattr(
        "repositories.postgres.memory.claim_memory_vector",
        lambda memory_id: claims.get(memory_id),
    )
    monkeypatch.setattr(
        "repositories.postgres.memory.complete_memory_vector",
        lambda memory_id, **kwargs: completed.append((memory_id, kwargs)) or True,
    )
    monkeypatch.setattr("core.llm_client.embed_text", lambda text: [1.0, 0.0, 0.0] if text == "alpha" else [0.0, 1.0, 0.0])

    summary = run_layer3_eval._complete_seed_memory_vectors(["m1", "m2"])

    assert summary["requested"] == 2
    assert summary["completed"] == 2
    assert summary["already_ready_or_inactive"] == 0
    assert summary["failed_count"] == 0
    assert [item[0] for item in completed] == ["m1", "m2"]
