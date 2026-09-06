"""Deterministically rescore P2 raw LLM outputs after evaluator-label fixes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path.cwd()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.eval_ask_stage_p2_react_chains import aggregate, build_cases, ordered_coverage, report_text, valid_args


def rescore(raw: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    calls = list(raw.get("calls") or [])
    observations: list[dict[str, Any]] = []
    for call in calls:
        call["args_ok"] = valid_args(
            str(call.get("action") or ""),
            call.get("args") if isinstance(call.get("args"), dict) else {},
            observations,
            str(raw.get("question") or ""),
        )
        result = next(
            (
                observation.get("result")
                for observation in raw.get("observations") or []
                if len(observations) == int(call.get("step") or 1) - 1
            ),
            [],
        )
        observations.append({"result": result})
    actual = [str(call.get("token") or "") for call in calls]
    required = list(expected["required"])
    coverage = sum(token in actual for token in required) / len(required)
    chain_complete = coverage == 1.0
    order_ok = ordered_coverage(required, actual) if expected["order_strict"] else chain_complete
    required_counts = {token: required.count(token) for token in set(required)}
    extra_calls = sum(max(0, actual.count(token) - required_counts.get(token, 0)) for token in set(actual))
    return {
        **raw,
        "required": required,
        "order_strict": expected["order_strict"],
        "calls": calls,
        "required_tool_coverage": round(coverage, 6),
        "chain_complete": chain_complete,
        "order_ok": order_ok,
        "args_ok": bool(calls) and all(bool(call.get("args_ok")) for call in calls),
        "premature_final": bool(raw.get("final_answer")) and not chain_complete,
        "extra_calls": extra_calls,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    expected = {case["case_id"]: case for case in build_cases()}
    raw_rows = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [rescore(row, expected[str(row["case_id"])]) for row in raw_rows]
    rows.sort(key=lambda row: row["case_id"])
    metrics = aggregate(rows)
    failures = [
        row
        for row in rows
        if not row["chain_complete"] or not row["order_ok"] or not row["args_ok"] or not row["terminated_with_final"]
    ]
    (output / "metrics_rescored.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (output / "cases_rescored.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "failures_rescored.jsonl").open("w", encoding="utf-8") as handle:
        for row in failures:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = report_text(metrics, failures).replace("# Ask P2 多步 ReAct 工具链报告", "# Ask P2 多步 ReAct 工具链重算报告")
    report += "\n> 重算只修正 memory subtype 与空 query 任务清单两项 Gold/参数口径；未重发 LLM，原始输出保持不变。\n"
    (output / "ASK_P2_REACT_CHAIN_EVALUATION_RESCORED.md").write_text(report, encoding="utf-8")
    print(json.dumps({"metrics": metrics, "failure_count": len(failures)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
