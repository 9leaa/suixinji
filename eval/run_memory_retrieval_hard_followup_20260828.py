"""Focused post-repair acceptance for ranking, temporal state, and orphan events."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from eval.run_memory_retrieval_acceptance_20260828 import (
    PREFERENCES,
    SEMANTICS,
    TASKS,
    _evaluate_case,
    write_json,
)
from memory.service import task_status_search


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "eval" / "results" / "memory_retrieval_acceptance_20260828"
JSON_PATH = BASE / "memory_retrieval_hard30_after.json"
REPORT_PATH = BASE / "MEMORY_RETRIEVAL_FOLLOWUP_REPORT_20260828.md"


def _load(name: str) -> Any:
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def _memory_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for index, (topic, _current, _previous, _facet) in enumerate(SEMANTICS):
        cases.append({
            "case_id": f"hard_semantic_{index}", "bucket": "semantic_hard",
            "query": f"截至现在，{topic}应以哪条记录为准？", "memory_type": "semantic",
            "expected": [f"semantic_{index}"], "must_not": [], "current_state": True,
            "spec_mode": "auto",
        })

    episode_queries = (
        "星河项目那场复盘会最后确认了什么？",
        "去植物园拍花的那次经历留下了什么记录？",
        "检索系统那场技术沙龙发生了什么？",
        "Agent简历模拟面试那次经历怎么样？",
        "数据库故障复盘最后记录了什么？",
        "去青岛的那次短途行程是什么时候？",
        "分布式系统阅读分享会是哪次经历？",
        "力量训练体验课那次经历发生了什么？",
        "随心记产品需求评审是哪次记录？",
        "和大学同学聚餐的记录是什么？",
    )
    for index, query in enumerate(episode_queries):
        cases.append({
            "case_id": f"hard_episode_{index}", "bucket": "episodic_hard",
            "query": query, "memory_type": "episodic", "expected": [f"episode_{index}"],
            "must_not": [], "spec_mode": "auto",
        })

    for index in (1, 3, 5):
        topic, state = TASKS[index]
        cases.append({
            "case_id": f"hard_task_{index}", "bucket": "no_regression",
            "query": f"麻烦只看当前状态，{topic}现在是否已经结束？", "memory_type": "task",
            "expected": [f"task_{index}"], "must_not": [f"task_{index}_old"],
            "expected_task_status": state, "current_state": True, "spec_mode": "auto",
        })
    for index in (0, 2):
        topic, _family, polarity, _text = PREFERENCES[index]
        cases.append({
            "case_id": f"hard_preference_{index}", "bucket": "no_regression",
            "query": f"不看旧记录，我现在对{topic}的偏好是什么？", "memory_type": "preference",
            "expected": [f"pref_{index}"], "must_not": [f"pref_{index}_old"],
            "expected_polarity": polarity, "spec_mode": "auto",
        })
    return cases


def _orphan_cases() -> list[dict[str, str]]:
    return [
        {"case_id": "hard_orphan_0", "query": "Agent简历模拟面试这件事现在怎么样了？", "expected": "episode_3"},
        {"case_id": "hard_orphan_1", "query": "数据库故障复盘这件事现在怎么样了？", "expected": "episode_4"},
        {"case_id": "hard_orphan_2", "query": "力量训练体验课这件事现在怎么样了？", "expected": "episode_7"},
        {"case_id": "hard_orphan_3", "query": "随心记产品需求评审这件事现在怎么样了？", "expected": "episode_8"},
        {"case_id": "hard_orphan_4", "query": "检索系统技术沙龙这件事现在怎么样了？", "expected": "episode_2"},
    ]


def _rank(alias: str, ranked: list[str]) -> int | None:
    try:
        return ranked.index(alias) + 1
    except ValueError:
        return None


def main() -> None:
    state = _load("memory_retrieval_checkpoint.json")
    records = state["records"]
    reverse = {str(record["id"]): alias for alias, record in records.items()}
    memory_results = [
        _evaluate_case(state["space_id"], case, records, reverse)
        for case in _memory_cases()
    ]

    orphan_results: list[dict[str, Any]] = []
    for case in _orphan_cases():
        started = time.perf_counter()
        rows = task_status_search(state["space_id"], case["query"], limit=8)
        aliases = [reverse.get(str(row.get("id") or ""), str(row.get("id") or "")) for row in rows]
        rank = _rank(case["expected"], aliases)
        orphan_results.append({
            **case,
            "ranked_aliases": aliases,
            "rank": rank,
            "passed": bool(rank and rank <= 5),
            "roles": [str(row.get("task_evidence_role") or "") for row in rows],
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        })

    strict_failures = [
        item for item in memory_results
        if (item["recall_at_5"] or 0.0) < 1.0
        or item["current_state_correct"] is False
        or item["task_state_correct"] is False
        or item["polarity_correct"] is False
        or item["must_not_violation"]
    ]
    strict_failures.extend(item for item in orphan_results if not item["passed"])
    all_ranks = [item["first_relevant_rank"] for item in memory_results]
    all_ranks.extend(item["rank"] for item in orphan_results)
    output = {
        "cases": 30,
        "memory_results": memory_results,
        "orphan_results": orphan_results,
        "metrics": {
            "top1_accuracy": statistics.fmean(rank == 1 for rank in all_ranks),
            "recall_at_3": statistics.fmean(bool(rank and rank <= 3) for rank in all_ranks),
            "recall_at_5": statistics.fmean(bool(rank and rank <= 5) for rank in all_ranks),
            "semantic_current_accuracy": statistics.fmean(
                item["current_state_correct"] is True
                for item in memory_results if item["bucket"] == "semantic_hard"
            ),
            "episodic_recall_at_5": statistics.fmean(
                item["recall_at_5"] for item in memory_results if item["bucket"] == "episodic_hard"
            ),
            "orphan_task_to_episode_at_5": statistics.fmean(item["passed"] for item in orphan_results),
            "strict_failure_count": len(strict_failures),
        },
        "strict_failures": strict_failures,
    }
    write_json(JSON_PATH, output)

    before = _load("memory_retrieval_followup_failed10.json")
    after = _load("memory_retrieval_followup_failed10_after.json")
    before_failures = sum(
        (item["recall_at_5"] or 0.0) < 1.0 or item["current_state_correct"] is False
        for item in before["results"]
    )
    after_failures = sum(
        (item["recall_at_5"] or 0.0) < 1.0 or item["current_state_correct"] is False
        for item in after["results"]
    )
    metrics = output["metrics"]
    lines = [
        "# Memory 检索后续修复验证报告",
        "",
        "模式：PostgreSQL + pgvector；真实 Embedding；无 Cross-Encoder；复用原 75 条隔离 Memory。",
        "",
        "## 结果",
        "",
        "| 指标 | 修复前 | 修复后 |",
        "|---|---:|---:|",
        f"| 原 10 条失败样本中的严格失败 | {before_failures}/10 | {after_failures}/10 |",
        f"| 新增 Hard-30 Top1 Accuracy | - | {metrics['top1_accuracy']:.2%} |",
        f"| 新增 Hard-30 Recall@3 / @5 | - | {metrics['recall_at_3']:.2%} / {metrics['recall_at_5']:.2%} |",
        f"| Semantic Current Accuracy | - | {metrics['semantic_current_accuracy']:.2%} |",
        f"| Episodic Recall@5 | - | {metrics['episodic_recall_at_5']:.2%} |",
        f"| Task→Orphan Episodic Recall@5 | - | {metrics['orphan_task_to_episode_at_5']:.2%} |",
        f"| 新增 Hard-30 严格失败 | - | {metrics['strict_failure_count']}/30 |",
        "",
        "## 修改对应的问题",
        "",
        "- task 查询使用主题重叠校验，可容忍中间插入修饰词；孤儿完成仍作为 Episodic 证据，不伪造 Task 状态。",
        "- Lexical 只使用身份性强词；‘参加、发生、记录’等泛词不再给候选增加第二路 RRF 票。",
        "- `现在/当前/目前/最新` 自动进入 current time mode；Semantic 以问题相关性和 `valid_from` 选当前事实，历史仍保留。",
        "- Semantic Projection 为空时，Ask 执行器使用同一时间规则兜底，并把旧证据标为 historical。",
        "",
        "## Hard-30 明细",
        "",
        "| Case | Top5 | 通过 |",
        "|---|---|---:|",
    ]
    for item in memory_results:
        passed = item not in strict_failures
        lines.append(f"| {item['case_id']} | {', '.join(item['ranked_aliases'][:5])} | {'是' if passed else '否'} |")
    for item in orphan_results:
        lines.append(f"| {item['case_id']} | {', '.join(item['ranked_aliases'][:5]) or '空'} | {'是' if item['passed'] else '否'} |")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(output["metrics"], ensure_ascii=False, indent=2))
    print(REPORT_PATH)


if __name__ == "__main__":
    main()
