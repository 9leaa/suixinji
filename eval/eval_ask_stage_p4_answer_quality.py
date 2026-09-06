"""P4 Ask evaluation: LLM-judge semantic correctness, completeness and grounding."""

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

from core.llm_client import complete_json

JUDGE_PROMPT = """
你是严格的 Ask 回答质量评测器。只根据给定 question、observations 和 expected_facts 判断 answer。
输出 JSON object：
{
  "correct": true,
  "complete": true,
  "grounded": true,
  "abstention_correct": true,
  "reason": "一句话理由"
}
定义：
- correct：结论与直接证据一致；同义改写算正确。
- complete：问题中的每个子问题都回答；单问题只需完整回答该问题。
- grounded：回答的具体事实均可由 observations 支撑，没有编造。
- abstention_correct：有直接证据时应回答；没有证据时应明确说无法确认且不得编造。
不要因语言风格、是否逐字复述、是否包含引用格式而扣分。
"""


def load_rows(p2_path: Path, p3_path: Path) -> list[dict[str, Any]]:
    rows = []
    for raw in p2_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        rows.append(
            {
                "case_id": item["case_id"],
                "source_stage": "P2",
                "group": item["group"],
                "question": item["question"],
                "observations": item.get("observations") or [],
                "expected_facts": [],
                "answer": item.get("final_answer") or "",
            }
        )
    for raw in p3_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        rows.append(
            {
                "case_id": item["case_id"],
                "source_stage": "P3",
                "group": item["group"],
                "question": item["question"],
                "observations": item.get("observations") or [],
                "expected_facts": item.get("expected_tokens") or [],
                "answer": item.get("answer") or "",
            }
        )
    return rows


def judge_case(case: dict[str, Any], retries: int) -> dict[str, Any]:
    payload = {
        "question": case["question"],
        "observations": case["observations"],
        "expected_facts": case["expected_facts"],
        "answer": case["answer"],
    }
    output = None
    error = None
    attempts = []
    started = time.perf_counter()
    for attempt in range(1, retries + 2):
        one = time.perf_counter()
        try:
            output = complete_json(
                JUDGE_PROMPT,
                json.dumps(payload, ensure_ascii=False, indent=2),
                model_role="strong",
                llm_task="evaluation_judge",
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
    flags = {key: output.get(key) is True for key in ("correct", "complete", "grounded", "abstention_correct")}
    return {
        **case,
        "judge": output,
        "judge_error": error,
        "judge_success": error is None and all(key in output for key in flags),
        **flags,
        "all_quality_pass": all(flags.values()),
        "attempts": attempts,
        "latency_s": round(time.perf_counter() - started, 4),
    }


def percentile(values: list[float], p: float) -> float:
    return sorted(values)[max(0, min(len(values) - 1, math.ceil(p * len(values)) - 1))] if values else 0.0


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def rate(items: list[dict[str, Any]], key: str) -> float:
        return round(sum(bool(row[key]) for row in items) / len(items), 6) if items else 0.0

    latencies = [float(row["latency_s"]) for row in rows if row["judge_success"]]
    by_source = {}
    for name in sorted({row["source_stage"] for row in rows}):
        bucket = [row for row in rows if row["source_stage"] == name]
        by_source[name] = {
            "cases": len(bucket),
            "correct": rate(bucket, "correct"),
            "complete": rate(bucket, "complete"),
            "grounded": rate(bucket, "grounded"),
            "all_quality_pass": rate(bucket, "all_quality_pass"),
        }
    return {
        "cases": len(rows),
        "judge_success_rate": rate(rows, "judge_success"),
        "answer_nonempty_rate": round(sum(bool(row["answer"].strip()) for row in rows) / len(rows), 6),
        "semantic_correct_rate": rate(rows, "correct"),
        "complete_rate": rate(rows, "complete"),
        "grounded_rate": rate(rows, "grounded"),
        "abstention_correct_rate": rate(rows, "abstention_correct"),
        "all_quality_pass_rate": rate(rows, "all_quality_pass"),
        "by_source": by_source,
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
    lines = [
        "# Ask P4 最终回答质量报告",
        "",
        "- 数据：P2 多步问题 30 条 + P3 固定证据问题 30 条。",
        "- 真实独立 Judge；按语义而非字符串判断正确、完整、忠于证据和拒答。",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| Judge 成功率 | {pct(metrics['judge_success_rate'])} |",
        f"| Answer 非空率 | {pct(metrics['answer_nonempty_rate'])} |",
        f"| 语义正确率 | {pct(metrics['semantic_correct_rate'])} |",
        f"| 回答完整率 | {pct(metrics['complete_rate'])} |",
        f"| Grounded 率 | {pct(metrics['grounded_rate'])} |",
        f"| 拒答行为正确率 | {pct(metrics['abstention_correct_rate'])} |",
        f"| 四项全通过率 | {pct(metrics['all_quality_pass_rate'])} |",
        f"| Judge 耗时 Mean/P50/P95 | {latency['mean']:.3f}/{latency['p50']:.3f}/{latency['p95']:.3f}s |",
        "",
        "## 分来源",
        "",
        "| 来源 | Cases | 正确 | 完整 | Grounded | 全通过 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for source, row in metrics["by_source"].items():
        lines.append(
            f"| {source} | {row['cases']} | {pct(row['correct'])} | {pct(row['complete'])} | {pct(row['grounded'])} | {pct(row['all_quality_pass'])} |"
        )
    lines += [
        "",
        "## 结论边界",
        "",
        f"- 严格失败 {len(failures)} 条，见 `failures.jsonl`。",
        "- Judge 与回答模型同源，可能偏宽松；P5 仍需真实端到端测试。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--p2-cases", required=True)
    parser.add_argument("--p3-cases", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--retries", type=int, default=1)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    cases = load_rows(Path(args.p2_cases), Path(args.p3_cases))
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(judge_case, case, max(0, args.retries)) for case in cases]
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(f"progress={index}/{len(futures)} pass={row['all_quality_pass']}", flush=True)
    rows.sort(key=lambda row: (row["source_stage"], row["case_id"]))
    metrics = aggregate(rows)
    failures = [row for row in rows if not row["judge_success"] or not row["all_quality_pass"]]
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "failures.jsonl").open("w", encoding="utf-8") as handle:
        for row in failures:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (output / "ASK_P4_ANSWER_QUALITY_EVALUATION.md").write_text(report_text(metrics, failures), encoding="utf-8")
    print(json.dumps({"metrics": metrics, "failure_count": len(failures)}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
