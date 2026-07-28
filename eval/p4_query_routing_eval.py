"""P4 query-routing/planning evaluation.

This evaluation is intentionally planner-only: it does not insert notes or
memories, does not call the answer LLM, and does not touch the application
database.  It measures whether simple questions stay on the fast path and
whether complex questions activate bounded rewrite/decomposition/step-back
planning.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.query_planner import build_query_plan


DATASET_PATH = ROOT / "eval" / "data" / "p4_query_routing_v1.json"
RESULTS_DIR = ROOT / "eval" / "results"

TOPICS = [
    "RAG混合检索", "SQL索引", "Agent简历", "Canonical Key", "Relation Guard",
    "飞书Pending", "燕麦拿铁", "Python异步", "订阅预算", "上海博物馆",
    "向量检索", "敏感笔记", "查询改写", "分布式系统", "周报整理",
]


def _cases() -> list[dict[str, Any]]:
    """负责“cases”。

    该函数是 `eval.p4_query_routing_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    cases: list[dict[str, Any]] = []
    serial = 0

    def add(style: str, query: str, expected: str, strategy: str | None = None) -> None:
        """负责“添加”。

        该函数是 `eval.p4_query_routing_eval` 中的`_cases` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        nonlocal serial
        cases.append(
            {
                "case_id": f"p4_{serial:04d}",
                "style": style,
                "query": query,
                "expected_complexity": expected,
                "expected_requires_expansion": expected == "complex",
                "expected_strategy": strategy,
            }
        )
        serial += 1

    # Twelve simple families x ten examples.  Several families deliberately
    # contain natural filler words or long/negated wording to expose false
    # positives in marker-based routing.
    for i in range(10):
        t = TOPICS[i % len(TOPICS)]
        add("simple_fact", f"{t}的当前记录是什么", "simple")
        add("simple_task", f"{t}这项任务状态", "simple")
        add("simple_note", f"关于{t}的笔记", "simple")
        add("simple_preference", f"我对{t}的偏好", "simple")
        add("simple_english", f"what is my current memory about {t}", "simple")
        add("simple_filler", f"请帮我查一下{t}", "simple")
        add("simple_short_question", f"{t}怎么记录", "simple")
        add("simple_code", f"记录编号 P4-{i:02d} 对应的{t}", "simple")
        add("simple_state_layer", f"状态记忆里的{t}", "simple")
        add("simple_single_entity", f"{t}这一条事实", "simple")
        add(
            "simple_long_single_topic",
            f"请从长期记忆中只查找{t}这一项主题的唯一当前结论并返回对应的一条记录，不要扩展到其他主题，不需要额外解释背景，也不需要拆分问题",
            "simple",
        )
        add(
            "simple_negated_markers",
            f"只查询{t}，不要比较其它主题，也不要分析原因",
            "simple",
        )

    # Complex families x ten examples.  The English, punctuation-only and
    # long-no-marker groups are adversarial: a robust planner should still
    # recognize their multi-hop intent.
    for i in range(10):
        a = TOPICS[(i * 2) % len(TOPICS)]
        b = TOPICS[(i * 2 + 1) % len(TOPICS)]
        c = TOPICS[(i * 2 + 2) % len(TOPICS)]
        add("complex_compare", f"比较{a}和{b}的当前结论，并说明适用场景", "complex", "decomposition")
        add("complex_causal", f"为什么{a}的状态发生变化，结合之前记录解释原因", "complex", "step_back")
        add("complex_multi_evidence", f"结合{a}、{b}以及{c}的记录，归纳当前结论", "complex", "decomposition")
        add("complex_multi_part", f"{a}现在什么状态，并且{b}是否完成，另外说明{c}的偏好", "complex", "decomposition")
        add("complex_trend", f"分析{a}前后变化和趋势，并指出证据", "complex", "step_back")
        add("complex_step_back", f"对{a}做step-back分析后再给出具体结论", "complex", "step_back")
        add(
            "complex_long_no_marker",
            f"请从长期记忆和普通笔记中检索{a}的最新状态以及相关证据，返回一份面向当前任务的完整判断并保留原始记录编号",
            "complex",
            "any",
        )
        add(
            "complex_english_why",
            f"Why did the status of {a} change, and what evidence explains the current conclusion?",
            "complex",
            "step_back",
        )
        add(
            "complex_english_compare",
            f"Compare {a} and {b} and explain which one is more suitable for the current task",
            "complex",
            "decomposition",
        )
        add(
            "complex_punctuation_multi",
            f"查{a}？再查{b}？最后把两条结果合并成一个结论",
            "complex",
            "decomposition",
        )
        add(
            "complex_rewrite",
            f"请帮我结合{a}的最新记录和{b}的历史记录，输出简洁结论",
            "complex",
            "rewrite",
        )
        add(
            "complex_relation",
            f"分别找出{a}与{b}的记录，分析它们之间的关联、差异和对当前决策的影响",
            "complex",
            "decomposition",
        )

    return cases


def _p95(values: list[float]) -> float:
    """负责“p95”。

    该函数是 `eval.p4_query_routing_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.95
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def _plan_dict(plan: Any) -> dict[str, Any]:
    """负责“规划dict”。

    该函数是 `eval.p4_query_routing_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return asdict(plan)


def run() -> tuple[Path, Path, dict[str, Any]]:
    """负责“运行”。

    该函数是 `eval.p4_query_routing_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    cases = _cases()
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    dataset = {
        "dataset": "p4_query_routing_v1",
        "description": "Planner-only routing set: 120 simple + 120 complex, including adversarial long, negated, English and punctuation queries.",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": cases,
    }
    DATASET_PATH.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")

    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    errors: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter_ns()
        try:
            plan = build_query_plan(case["query"])
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            payload = _plan_dict(plan)
            variant_count = len(plan.retrieval_queries)
            expansion = bool(
                variant_count
                or plan.use_query_rewrite
                or plan.use_decomposition
                or plan.use_step_back
            )
            strategy = case["expected_strategy"]
            if strategy == "decomposition":
                strategy_hit = bool(plan.use_decomposition)
            elif strategy == "step_back":
                strategy_hit = bool(plan.use_step_back)
            elif strategy == "rewrite":
                strategy_hit = bool(plan.use_query_rewrite)
            elif strategy == "any":
                strategy_hit = expansion
            else:
                strategy_hit = None
            rows.append(
                {
                    **case,
                    "actual_complexity": plan.complexity,
                    "complexity_correct": plan.complexity == case["expected_complexity"],
                    "variant_count": variant_count,
                    "expansion": expansion,
                    "strategy_hit": strategy_hit,
                    "latency_ms": elapsed_ms,
                    "plan": payload,
                }
            )
            latencies.append(elapsed_ms)
        except Exception as exc:  # pragma: no cover - retained in the report
            errors.append({"case_id": case["case_id"], "error": repr(exc)})

    simple = [r for r in rows if r["expected_complexity"] == "simple"]
    complex_rows = [r for r in rows if r["expected_complexity"] == "complex"]
    correct = sum(r["complexity_correct"] for r in rows)
    simple_false_complex = sum(r["actual_complexity"] == "complex" for r in simple)
    complex_false_simple = sum(r["actual_complexity"] == "simple" for r in complex_rows)
    simple_unnecessary = sum(r["expansion"] for r in simple)
    complex_no_expansion = sum(not r["expansion"] for r in complex_rows)

    tp = sum(r["actual_complexity"] == "complex" for r in complex_rows)
    fp = simple_false_complex
    fn = complex_false_simple
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    strategy_stats: dict[str, dict[str, Any]] = {}
    for strategy in ("decomposition", "step_back", "rewrite", "any"):
        subset = [r for r in complex_rows if r["expected_strategy"] == strategy]
        hits = sum(bool(r["strategy_hit"]) for r in subset)
        strategy_stats[strategy] = {"cases": len(subset), "hits": hits, "recall": hits / len(subset) if subset else None}

    by_style: dict[str, dict[str, Any]] = {}
    for style in sorted({r["style"] for r in rows}):
        subset = [r for r in rows if r["style"] == style]
        by_style[style] = {
            "cases": len(subset),
            "classification_accuracy": sum(r["complexity_correct"] for r in subset) / len(subset),
            "expansion_rate": sum(r["expansion"] for r in subset) / len(subset),
            "avg_variants": statistics.mean(r["variant_count"] for r in subset),
        }

    metrics: dict[str, Any] = {
        "total_cases": len(cases),
        "executed_cases": len(rows),
        "errors": len(errors),
        "classification_accuracy": correct / len(rows) if rows else 0.0,
        "complex_precision": precision,
        "complex_recall": recall,
        "complex_f1": f1,
        "simple_false_complex_rate": simple_false_complex / len(simple) if simple else 0.0,
        "complex_false_simple_rate": complex_false_simple / len(complex_rows) if complex_rows else 0.0,
        "simple_unnecessary_expansion_rate": simple_unnecessary / len(simple) if simple else 0.0,
        "complex_no_expansion_rate": complex_no_expansion / len(complex_rows) if complex_rows else 0.0,
        "complex_expansion_coverage": 1 - (complex_no_expansion / len(complex_rows) if complex_rows else 0.0),
        "avg_variant_count": statistics.mean(r["variant_count"] for r in rows) if rows else 0.0,
        "max_variant_count": max((r["variant_count"] for r in rows), default=0),
        "strategy": strategy_stats,
        "activation_rates_on_complex": {
            "query_rewrite": sum(r["plan"]["use_query_rewrite"] for r in complex_rows) / len(complex_rows) if complex_rows else 0.0,
            "decomposition": sum(r["plan"]["use_decomposition"] for r in complex_rows) / len(complex_rows) if complex_rows else 0.0,
            "step_back": sum(r["plan"]["use_step_back"] for r in complex_rows) / len(complex_rows) if complex_rows else 0.0,
        },
        "latency_ms": {
            "avg": statistics.mean(latencies) if latencies else 0.0,
            "p50": statistics.median(latencies) if latencies else 0.0,
            "p95": _p95(latencies),
            "max": max(latencies, default=0.0),
        },
        "llm_calls": 0,
        "llm_calls_note": "build_query_plan is deterministic and does not call the LLM; this is a planner-only measurement.",
        "by_style": by_style,
    }

    failures = [r for r in rows if not r["complexity_correct"] or (r["expected_requires_expansion"] and not r["expansion"]) or (not r["expected_requires_expansion"] and r["expansion"])]
    output = {
        "dataset": dataset["dataset"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "errors": errors,
        "failure_examples": failures[:40],
        "rows": rows,
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path = RESULTS_DIR / f"p4_query_routing_{stamp}.json"
    markdown_path = RESULTS_DIR / f"p4_query_routing_{stamp}.md"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    s = metrics["strategy"]
    md = (
        "# P4 Query Routing / Planning Evaluation\n\n"
        f"- Dataset: `eval/data/p4_query_routing_v1.json` ({metrics['total_cases']} cases; 120 simple + 120 complex)\n"
        "- Scope: deterministic `build_query_plan` only; no notes/memories inserted and no LLM answer calls.\n"
        f"- Executed: {metrics['executed_cases']}; errors: {metrics['errors']}\n\n"
        "| Metric | Value |\n|---|---:|\n"
        f"| Complexity accuracy | {metrics['classification_accuracy']:.3f} |\n"
        f"| Complex precision / recall / F1 | {metrics['complex_precision']:.3f} / {metrics['complex_recall']:.3f} / {metrics['complex_f1']:.3f} |\n"
        f"| Simple → complex (false complex) | {metrics['simple_false_complex_rate']:.3f} |\n"
        f"| Complex → simple (false simple) | {metrics['complex_false_simple_rate']:.3f} |\n"
        f"| Simple unnecessary expansion | {metrics['simple_unnecessary_expansion_rate']:.3f} |\n"
        f"| Complex expansion coverage | {metrics['complex_expansion_coverage']:.3f} |\n"
        f"| Decomposition recall (expected subset) | {s['decomposition']['recall']:.3f} |\n"
        f"| Step-back recall (expected subset) | {s['step_back']['recall']:.3f} |\n"
        f"| Query-rewrite recall (expected subset) | {s['rewrite']['recall']:.3f} |\n"
        f"| Planner latency avg / p95 | {metrics['latency_ms']['avg']:.3f} / {metrics['latency_ms']['p95']:.3f} ms |\n"
        f"| LLM calls | {metrics['llm_calls']} |\n\n"
        "## Interpretation\n\n"
        "- The simple/complex split is evaluated against semantic gold labels, including adversarial long, negated, English and punctuation cases.\n"
        "- Expansion is a plan-level signal (a variant or planner flag); the live query agent applies an additional complexity/quality gate before executing variants.\n"
        "- See the JSON for every plan and the first 40 failures; this report is a development evaluation, not a final holdout.\n"
    )
    markdown_path.write_text(md, encoding="utf-8")
    return DATASET_PATH, result_path, metrics


if __name__ == "__main__":
    dataset_path, result_path, metrics = run()
    print(json.dumps({"dataset": str(dataset_path), "result": str(result_path), "metrics": metrics}, ensure_ascii=False, indent=2))
