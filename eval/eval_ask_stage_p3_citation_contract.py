"""P3 Ask evaluation: final-answer evidence citation contract with fixed observations."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any

ROOT = Path.cwd()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.query_agent import REACT_SYSTEM_PROMPT
from core.llm_client import complete_json

NO_ANSWER_MARKERS = ("没有找到", "无法确认", "没有足够", "暂无", "未找到")


def build_cases() -> list[dict[str, Any]]:
    templates = [
        ("note", "我记录的{topic}结论是什么？", "search_notes", "Note 中记录：{fact}"),
        ("preference", "我现在对{topic}有什么偏好？", "search_memories", "用户偏好：{fact}"),
        ("semantic", "关于{topic}，我的长期事实是什么？", "search_memories", "用户事实：{fact}"),
        ("episodic", "我关于{topic}经历过什么？", "search_memories", "用户经历：{fact}"),
        ("task", "{topic}任务现在是什么状态？", "search_tasks", "任务状态：{fact}"),
    ]
    values = {
        "note": [("RAG 分块", "采用按标题和语义边界分块"), ("部署", "服务使用 Docker 启动"), ("日志", "需要记录 trace_id"), ("评测", "使用冻结测试集"), ("数据库", "使用 PostgreSQL")],
        "preference": [("报告", "喜欢简洁报告"), ("咖啡", "不喜欢甜咖啡"), ("回答", "偏好先给结论"), ("早餐", "避开牛奶"), ("饮料", "喜欢崂山可乐")],
        "semantic": [("居住地", "现在住在上海"), ("工作", "主要负责 RAG 检索质量"), ("简历", "方向是 Agent 开发"), ("学习", "重点学习长期记忆"), ("项目", "正在开发随心记")],
        "episodic": [("植物园", "周末去了植物园并拍花"), ("复盘会", "参加了记忆验收复盘会"), ("面试", "参加了字节一面"), ("旅行", "去青岛旅行"), ("答辩", "完成了本科毕业论文答辩")],
        "task": [("部署文档", "done"), ("截图整理", "todo"), ("供应商切换", "done"), ("Agent 简历", "done"), ("RAG 报告", "todo")],
    }
    cases = []
    index = 0
    for group, question_template, tool, content_template in templates:
        for topic, fact in values[group]:
            index += 1
            gold_id = f"gold_{index:03d}"
            distractor_id = f"noise_{index:03d}"
            cases.append(
                {
                    "case_id": f"p3_{index:03d}",
                    "group": group,
                    "question": question_template.format(topic=topic),
                    "expected_tokens": [fact],
                    "gold_ids": [gold_id],
                    "observations": [
                        {
                            "tool": tool,
                            "args": {"query": topic},
                            "result": [
                                {"id": gold_id, "content": content_template.format(fact=fact), "score": 0.96},
                                {"id": distractor_id, "content": f"与{topic}无关的干扰事实", "score": 0.31},
                            ],
                        }
                    ],
                }
            )
    for offset in range(5):
        index += 1
        cases.append(
            {
                "case_id": f"p3_{index:03d}",
                "group": "no_answer",
                "question": f"我是否记录过不存在事项{offset + 1}？",
                "expected_tokens": [],
                "gold_ids": [],
                "observations": [{"tool": "search_memories", "args": {"query": f"不存在事项{offset + 1}"}, "result": []}],
            }
        )
    return cases


def observation_ids(observations: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("id") or item.get("memory_id") or "")
        for observation in observations
        for item in (observation.get("result") if isinstance(observation.get("result"), list) else [observation.get("result")])
        if isinstance(item, dict) and (item.get("id") or item.get("memory_id"))
    }


def evaluate_case(case: dict[str, Any], retries: int) -> dict[str, Any]:
    payload = {"question": case["question"], "step": 2, "observations": case["observations"]}
    output = None
    error = None
    attempts = []
    started = time.perf_counter()
    for attempt in range(1, retries + 2):
        one = time.perf_counter()
        try:
            output = complete_json(
                REACT_SYSTEM_PROMPT,
                json.dumps(payload, ensure_ascii=False, indent=2),
                model_role="strong",
                llm_task="query_complex_reasoning",
                timeout_seconds=45,
            )
            attempts.append({"attempt": attempt, "ok": True, "latency_s": round(time.perf_counter() - one, 4)})
            error = None
            break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            attempts.append(
                {"attempt": attempt, "ok": False, "latency_s": round(time.perf_counter() - one, 4), "error": error}
            )
    output = output if isinstance(output, dict) else {}
    answer = str(output.get("final_answer") or "").strip()
    raw_ids = output.get("evidence_ids")
    evidence_ids = [str(value) for value in raw_ids] if isinstance(raw_ids, list) else []
    authorized = observation_ids(case["observations"])
    gold = set(case["gold_ids"])
    cited = set(evidence_ids)
    answerable = bool(gold)
    precision = len(cited & authorized) / len(cited) if cited else (0.0 if answerable else 1.0)
    recall = len(cited & gold) / len(gold) if gold else float(not cited)
    expected_ok = all(token in answer for token in case["expected_tokens"])
    abstain_ok = any(marker in answer for marker in NO_ANSWER_MARKERS) if not answerable else True
    return {
        **case,
        "output": output,
        "answer": answer,
        "evidence_ids": evidence_ids,
        "authorized_ids": sorted(authorized),
        "error": error,
        "request_ok": error is None,
        "final_answer_present": bool(answer),
        "citation_present": bool(cited) if answerable else not cited,
        "authorized_precision": round(precision, 6),
        "gold_recall": round(recall, 6),
        "exact_citation_set": cited == gold,
        "unauthorized_citation": bool(cited - authorized),
        "distractor_citation": bool((authorized - gold) & cited),
        "expected_fact_present": expected_ok,
        "no_answer_abstention_ok": abstain_ok,
        "attempts": attempts,
        "latency_s": round(time.perf_counter() - started, 4),
    }


def percentile(values: list[float], p: float) -> float:
    return sorted(values)[max(0, min(len(values) - 1, math.ceil(p * len(values)) - 1))] if values else 0.0


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    answerable = [row for row in rows if row["gold_ids"]]
    no_answer = [row for row in rows if not row["gold_ids"]]

    def rate(items: list[dict[str, Any]], key: str) -> float:
        return round(sum(bool(row[key]) for row in items) / len(items), 6) if items else 0.0

    latencies = [float(row["latency_s"]) for row in rows if row["request_ok"]]
    return {
        "cases": total,
        "answerable_cases": len(answerable),
        "no_answer_cases": len(no_answer),
        "request_success_rate": rate(rows, "request_ok"),
        "final_answer_present_rate": rate(rows, "final_answer_present"),
        "citation_presence_rate": rate(answerable, "citation_present"),
        "authorized_citation_precision": round(statistics.fmean(row["authorized_precision"] for row in answerable), 6),
        "gold_citation_recall": round(statistics.fmean(row["gold_recall"] for row in answerable), 6),
        "exact_citation_set_rate": rate(answerable, "exact_citation_set"),
        "unauthorized_citation_rate": rate(answerable, "unauthorized_citation"),
        "distractor_citation_rate": rate(answerable, "distractor_citation"),
        "no_answer_empty_citation_rate": rate(no_answer, "citation_present"),
        "no_answer_abstention_rate": rate(no_answer, "no_answer_abstention_ok"),
        "expected_fact_presence_rate": rate(answerable, "expected_fact_present"),
        "latency_s": {
            "mean": round(statistics.fmean(latencies), 4) if latencies else 0.0,
            "p50": round(percentile(latencies, 0.50), 4),
            "p95": round(percentile(latencies, 0.95), 4),
        },
    }


def pct(value: float) -> str:
    return f"{value:.2%}"


def report_text(metrics: dict[str, Any], failures: list[dict[str, Any]]) -> str:
    latency = metrics["latency_s"]
    return "\n".join(
        [
            "# Ask P3 引用契约报告",
            "",
            f"- 固定正确 evidence：{metrics['answerable_cases']} 条；无答案：{metrics['no_answer_cases']} 条。",
            "- 真实 LLM；不执行检索，只测 Final Answer 与 evidence_ids。",
            "",
            "| 指标 | 结果 |",
            "|---|---:|",
            f"| 请求成功率 | {pct(metrics['request_success_rate'])} |",
            f"| Final Answer 非空率 | {pct(metrics['final_answer_present_rate'])} |",
            f"| 可回答题引用存在率 | {pct(metrics['citation_presence_rate'])} |",
            f"| 授权引用 Precision | {pct(metrics['authorized_citation_precision'])} |",
            f"| Gold 引用 Recall | {pct(metrics['gold_citation_recall'])} |",
            f"| 引用集合 Exact | {pct(metrics['exact_citation_set_rate'])} |",
            f"| 越权 ID 率 | {pct(metrics['unauthorized_citation_rate'])} |",
            f"| 干扰证据误引率 | {pct(metrics['distractor_citation_rate'])} |",
            f"| 无答案空引用率 | {pct(metrics['no_answer_empty_citation_rate'])} |",
            f"| 无答案拒答正确率 | {pct(metrics['no_answer_abstention_rate'])} |",
            f"| 预期事实出现率（P4 预诊断） | {pct(metrics['expected_fact_presence_rate'])} |",
            f"| 耗时 Mean/P50/P95 | {latency['mean']:.3f}/{latency['p50']:.3f}/{latency['p95']:.3f}s |",
            "",
            "## 结论边界",
            "",
            f"- 严格失败 {len(failures)} 条，见 `failures.jsonl`。",
            "- 本阶段只证明引用合同，不证明真实检索能找到这些证据。",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--retries", type=int, default=1)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(evaluate_case, case, max(0, args.retries)) for case in cases]
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(f"progress={index}/{len(futures)} exact={row['exact_citation_set']}", flush=True)
    rows.sort(key=lambda row: row["case_id"])
    metrics = aggregate(rows)
    failures = [
        row
        for row in rows
        if not row["request_ok"]
        or not row["final_answer_present"]
        or not row["exact_citation_set"]
        or not row["no_answer_abstention_ok"]
    ]
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "failures.jsonl").open("w", encoding="utf-8") as handle:
        for row in failures:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (output / "ASK_P3_CITATION_CONTRACT_EVALUATION.md").write_text(report_text(metrics, failures), encoding="utf-8")
    print(json.dumps({"metrics": metrics, "failure_count": len(failures)}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
