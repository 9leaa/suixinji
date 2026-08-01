"""文件作用：评测公共工具。

项目关系：本文件依赖 `eval.common`；被 暂无静态导入方或仅作为入口脚本执行。
"""


import json

from eval.common import (
    aggregate_boolean_scores,
    hit_at_k,
    load_jsonl,
    recall_at_k,
    score_classification,
    score_query_react,
    score_retrieval,
    score_summary,
)


def test_score_classification_accepts_multiple_types_and_requires_two_tag_hits():
    """函数功能：`test_score_classification_accepts_multiple_types_and_requires_two_tag_hits` 负责验证 score classification accepts multiple types and requires two tag hits 场景，服务于本文件职责：评测公共工具。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    score = score_classification(
        {"type": "任务", "tags": ["提醒", "待办"]},
        {
            "case_id": "c1",
            "acceptable_types": ["任务", "生活"],
            "expected_tags_any": ["提醒", "待办", "计划"],
        },
    )

    assert score["passed"] is True
    assert score["type_ok"] is True
    assert score["tags_any_hits"] == 2
    assert score["min_tag_hits"] == 2


def test_score_classification_fails_when_only_one_tag_hits():
    """函数功能：`test_score_classification_fails_when_only_one_tag_hits` 负责验证 score classification fails when only one tag hits 场景，服务于本文件职责：评测公共工具。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    score = score_classification(
        {"type": "任务", "tags": ["提醒", "检查"]},
        {
            "case_id": "c1",
            "acceptable_types": ["任务"],
            "expected_tags_any": ["提醒", "待办", "计划"],
        },
    )

    assert score["passed"] is False
    assert score["type_ok"] is True
    assert score["tags_any_ok"] is False
    assert score["tags_any_hits"] == 1


def test_score_classification_fails_on_unacceptable_type():
    """函数功能：`test_score_classification_fails_on_unacceptable_type` 负责验证 score classification fails on unacceptable type 场景，服务于本文件职责：评测公共工具。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    score = score_classification(
        {"type": "生活", "tags": ["提醒", "待办"]},
        {"case_id": "c1", "acceptable_types": ["任务"], "expected_tags_any": ["提醒", "待办"]},
    )

    assert score["passed"] is False
    assert score["type_ok"] is False


def test_hit_recall_and_score_retrieval_multi_answer():
    """函数功能：`test_hit_recall_and_score_retrieval_multi_answer` 负责验证 hit recall and score retrieval multi answer 场景，服务于本文件职责：评测公共工具。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    ranked_ids = ["n3", "n2", "n1", "n4"]
    assert hit_at_k(ranked_ids, ["n2"], 1) is False
    assert hit_at_k(ranked_ids, ["n2"], 2) is True
    assert recall_at_k(ranked_ids, ["n2", "n1", "n5"], 3) == 2 / 3

    score = score_retrieval(
        ranked_ids,
        {"case_id": "q1", "expected_note_ids": ["n2", "n1", "n5"], "pass_k": 3, "min_recall": 0.66},
    )
    assert score["hit@1"] is False
    assert score["hit@3"] is True
    assert score["recall@3"] == 0.6667
    assert score["passed"] is True


def test_score_retrieval_no_result_uses_min_score():
    """函数功能：`test_score_retrieval_no_result_uses_min_score` 负责验证 score retrieval no result uses min score 场景，服务于本文件职责：评测公共工具。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    passed = score_retrieval(
        ["n1", "n2"],
        {"case_id": "no1", "expected_no_result": True, "expected_note_ids": [], "min_score": 0.55},
        scores_by_id={"n1": 0.41, "n2": 0.2},
    )
    assert passed["passed"] is True
    assert passed["no_result_ok"] is True

    failed = score_retrieval(
        ["n1", "n2"],
        {"case_id": "no2", "expected_no_result": True, "expected_note_ids": [], "min_score": 0.55},
        scores_by_id={"n1": 0.7, "n2": 0.2},
    )
    assert failed["passed"] is False
    assert failed["no_result_ok"] is False


def test_score_query_react_checks_tools_notes_and_answer_terms():
    """函数功能：`test_score_query_react_checks_tools_notes_and_answer_terms` 负责验证 score query react checks tools notes and answer terms 场景，服务于本文件职责：评测公共工具。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    score = score_query_react(
        [
            {
                "tool": "filter_notes",
                "result": [{"id": "n1", "title": "任务"}],
            }
        ],
        "找到了任务。",
        {
            "case_id": "query1",
            "expected_tools_any": ["filter_notes"],
            "expected_note_ids": ["n1"],
            "answer_must_include": ["任务"],
        },
    )

    assert score["passed"] is True
    assert score["tools_any_ok"] is True
    assert score["notes_ok"] is True
    assert score["answer_ok"] is True


def test_score_summary_checks_required_and_forbidden_terms():
    """函数功能：`test_score_summary_checks_required_and_forbidden_terms` 负责验证 score summary checks required and forbidden terms 场景，服务于本文件职责：评测公共工具。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    score = score_summary(
        "今天记录了馅饼和 P4 自动总结测试。",
        {"case_id": "s1", "must_include": ["馅饼", "P4"], "must_not_include": ["会议"]},
    )
    assert score["passed"] is True

    failed = score_summary(
        "今天记录了馅饼和会议。",
        {"case_id": "s2", "must_include": ["馅饼", "P4"], "must_not_include": ["会议"]},
    )
    assert failed["passed"] is False
    assert failed["missing"] == ["P4"]
    assert failed["forbidden"] == ["会议"]


def test_aggregate_boolean_scores():
    """函数功能：`test_aggregate_boolean_scores` 负责验证 aggregate boolean scores 场景，服务于本文件职责：评测公共工具。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    summary = aggregate_boolean_scores([
        {"passed": True},
        {"passed": False},
        {"passed": True},
    ])

    assert summary == {"total": 3, "passed": 2, "failed": 1, "pass_rate": 0.6667}


def test_load_jsonl(tmp_path):
    """函数功能：`test_load_jsonl` 负责验证 load jsonl 场景，服务于本文件职责：评测公共工具。
    传参：
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    path = tmp_path / "cases.jsonl"
    lines = [
        json.dumps({"case_id": "a"}),
        "",
        json.dumps({"case_id": "b"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert load_jsonl(path) == [{"case_id": "a"}, {"case_id": "b"}]
