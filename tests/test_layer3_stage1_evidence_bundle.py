"""文件作用：Layer3 Stage 1 证据 bundle 接线测试。

项目关系：本文件依赖 `agent.query_agent`；被 暂无静态导入方或仅作为入口脚本执行。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent import query_agent


def test_answer_question_result_reads_structured_evidence_bundle(monkeypatch):
    """函数功能：`test_answer_question_result_reads_structured_evidence_bundle` 负责验证 answer question result reads structured evidence bundle 场景，服务于本文件职责：Layer3 Stage 1 证据 bundle 接线测试。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(query_agent, "_deterministic_route", lambda _question: None)
    monkeypatch.setattr(
        query_agent,
        "_memory_search_compat",
        lambda *_args, **_kwargs: [
            {"id": "m2", "memory_id": "m2", "content": "irrelevant", "sources": []},
            {"id": "m1", "memory_id": "m1", "content": "selected", "sources": [{"note_id": "s1"}]},
        ],
    )
    monkeypatch.setattr(query_agent, "memory_history", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        query_agent,
        "_run_answer_question_with_context",
        lambda *_args, **_kwargs: (
            "selected answer",
            SimpleNamespace(
                metadata={
                    "answer_evidence_bundle": {
                        "items": [
                            {
                                "kind": "memory",
                                "id": "m1",
                                "memory_id": "m1",
                                "source_ids": ["s1"],
                                "selected": True,
                                "role": "current",
                                "tool": "memory_search",
                            }
                        ],
                        "selected_context_refs": ["m1"],
                        "selected_tool_refs": ["memory_search"],
                        "executed_tools": ["memory_search"],
                    }
                }
            ),
        ),
    )

    result = query_agent.answer_question_result("space-1", "我喜欢什么？")

    assert result.answer_type == "answered"
    assert result.reason_code == "evidence_supported"
    assert result.selected_memory_ids == ["m1"]
    assert result.selected_context_refs == ["m1"]
    assert result.selected_tool_refs == ["memory_search"]
    assert result.executed_tools == ["memory_search"]
    assert result.evidence_bundle is not None
    assert result.evidence_bundle.selected_context_refs == ["m1"]


def test_answer_question_result_keeps_direct_history_answers_as_answered(monkeypatch):
    """函数功能：`test_answer_question_result_keeps_direct_history_answers_as_answered` 负责验证 answer question result keeps direct history answers as answered 场景，服务于本文件职责：Layer3 Stage 1 证据 bundle 接线测试。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(query_agent, "_deterministic_route", lambda _question: {"action": "memory_history"})
    monkeypatch.setattr(query_agent, "_memory_search_compat", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        query_agent,
        "memory_history",
        lambda *_args, **_kwargs: [
            {"id": "v1", "memory_id": "m1", "history": True, "content": "2024年1月", "sources": [{"note_id": "s1"}]}
        ],
    )
    monkeypatch.setattr(
        query_agent,
        "_run_answer_question_with_context",
        lambda *_args, **_kwargs: (
            "2024年1月",
            SimpleNamespace(
                metadata={
                    "answer_evidence_bundle": {
                        "items": [
                            {
                                "kind": "version",
                                "id": "v1",
                                "memory_id": "m1",
                                "version_id": "v1",
                                "source_ids": ["s1"],
                                "selected": True,
                                "role": "history",
                                "tool": "memory_history",
                            }
                        ],
                        "selected_context_refs": ["v1"],
                        "selected_tool_refs": ["memory_history"],
                        "executed_tools": ["memory_history"],
                    }
                }
            ),
        ),
    )

    result = query_agent.answer_question_result("space-1", "去年什么时候？")

    assert result.answer_type == "answered"
    assert result.reason_code == "history_query"
    assert result.evidence_mode == "history"
    assert result.selected_version_ids == ["v1"]
    assert result.selected_context_refs == ["v1"]
    assert result.selected_tool_refs == ["memory_history"]
