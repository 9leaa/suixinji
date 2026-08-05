"""Run all Layer-1 datasets in rules or authoritative hybrid-LLM mode.

Hybrid mode is deliberately fail-closed for evaluation: the authoritative LLM
must return a valid parsed result; no rule fallback is silently counted as LLM.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.layer1.run_regression import (  # noqa: E402
    candidate_metrics,
    field_metrics,
    gold_candidates,
    match,
    predicted,
    should_store_metrics,
)
from memory import extractor  # noqa: E402

ARCHIVES = (
    "suixinji_layer1_repaired_datasets.zip",
    "suixinji_layer1_remaining_datasets.zip",
)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _load_all(root: Path) -> dict[str, list[dict[str, Any]]]:
    data: dict[str, list[dict[str, Any]]] = {}
    for archive_name in ARCHIVES:
        archive_path = root / archive_name
        with zipfile.ZipFile(archive_path) as archive:
            for name in archive.namelist():
                if not name.endswith(".jsonl"):
                    continue
                rows = [json.loads(line) for line in archive.read(name).decode("utf-8").splitlines() if line.strip()]
                if not rows:
                    continue
                dataset = str(rows[0].get("dataset") or Path(name).stem)
                if dataset in data:
                    raise ValueError(f"duplicate dataset: {dataset}")
                data[dataset] = rows
    return dict(sorted(data.items()))


def _failure_details(raw_gold: list[dict[str, Any]], gold: list[dict[str, Any]], pred_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    gold_store = any(item.get("should_store", True) for item in raw_gold)
    if gold_store != bool(pred_rows):
        details.append({"kind": "should_store", "gold": gold_store, "pred": bool(pred_rows)})
    pairs, extras = match(gold, pred_rows)
    for index, (expected, actual) in enumerate(pairs):
        if actual is None:
            details.append({"kind": "missing_candidate", "index": index, "gold": expected})
            continue
        fields = [field for field, expected_value in expected.items() if field != "evidence_span" and actual.get(field) != expected_value]
        if fields:
            details.append({"kind": "field_mismatch", "index": index, "fields": fields, "gold": expected, "pred": actual})
    for item in extras:
        details.append({"kind": "extra_candidate", "pred": item})
    return details


def _run_case(row: dict[str, Any], mode: str) -> dict[str, Any]:
    note_id = str(row["case_id"])
    text = str(row["input"]["text"])
    classification = row["input"].get("classification")
    started = time.perf_counter()
    error: dict[str, Any] | None = None
    raw_candidates: list[Any] = []
    llm_attempted = mode == "hybrid" and not extractor._should_skip_text(extractor._strip_diagnostic_prefix(text))
    try:
        if mode == "rules":
            raw_candidates = extractor.extract_rule_candidates(note_id, text, classification)
        else:
            # This is the production hybrid algorithm's authoritative LLM call:
            # rules create hints, while LLM remains the only output authority.
            raw_candidates = extractor.extract_llm_candidates(
                note_id,
                text,
                classification,
                hints=extractor._rule_hints(note_id, text, classification),
            )
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
    latency_ms = int((time.perf_counter() - started) * 1000)
    pred_rows = [predicted(item) for item in raw_candidates]
    gold = gold_candidates(row)
    result = {
        "case_id": note_id,
        "dataset": row.get("dataset"),
        "difficulty": row.get("difficulty"),
        "coverage_tags": row.get("coverage_tags", []),
        "input": row.get("input"),
        "raw_gold": row.get("expected_output", {}).get("candidates", []),
        "gold": gold,
        "pred": pred_rows,
        # Candidate dataclass serialisation preserves every parsed extraction
        # field used by the runtime, without falsely claiming raw LLM text.
        "raw_candidate_output": _jsonable(raw_candidates),
        "latency_ms": latency_ms,
        "llm_attempted": llm_attempted if mode == "hybrid" else None,
        "llm_success": (error is None if llm_attempted else None),
        "error": error,
    }
    result["failure_details"] = _failure_details(result["raw_gold"], gold, pred_rows)
    return result


def _percentile(values: list[int], quantile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, int((len(values) - 1) * quantile)))
    return float(values[index])


def _summarize(rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    latencies = [int(row["latency_ms"]) for row in rows]
    result = {
        "cases": len(rows),
        "candidate": candidate_metrics(rows),
        "fields": field_metrics(rows),
        "should_store": should_store_metrics(rows),
        "failure_cases": sum(bool(row["failure_details"]) or row["error"] is not None for row in rows),
        "runtime": {
            "latency_ms_p50": _percentile(latencies, 0.50),
            "latency_ms_p95": _percentile(latencies, 0.95),
            "latency_ms_mean": statistics.fmean(latencies) if latencies else None,
        },
    }
    if mode == "hybrid":
        calls = sum(row["llm_attempted"] is True for row in rows)
        success = sum(row["llm_success"] is True for row in rows)
        result["llm"] = {
            "calls": calls,
            "success": success,
            "failed": calls - success,
            "success_rate": success / calls if calls else 1.0,
        }
    return result


def _markdown(mode: str, summaries: dict[str, dict[str, Any]], output_json: Path) -> str:
    lines = [
        f"# Layer 1 全量评测 — {mode}",
        "",
        "- 数据集：两个压缩包中的全部 5 个 dataset。",
        "- `hybrid`：规则 hints + 真实 LLM 权威抽取；LLM 失败不会降级为 Rules，也不会计为 LLM 成功。",
        "- 所有逐 case 输出在 `cases.jsonl`，失败样本在 `failures.jsonl`。",
        "",
        "| Dataset | Cases | Should-store F1 | Candidate F1 | Type Macro-F1 | Key-field Accuracy | All-fields Exact | Failure Cases |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for dataset, summary in summaries.items():
        fields = summary["fields"]
        all_exact = fields["all_fields_exact"]
        lines.append(
            f"| {dataset} | {summary['cases']} | {summary['should_store']['should_store_f1']:.2%} | "
            f"{summary['candidate']['candidate_f1']:.2%} | {summary['candidate']['memory_type_macro_f1']:.2%} | "
            f"{fields['key_field_accuracy']:.2%} | {all_exact:.2%} | {summary['failure_cases']} |"
        )
    lines.extend(["", f"完整 JSON：`{output_json}`"])
    if mode == "hybrid":
        lines.extend(["", "| Dataset | LLM Success | LLM Failure | LLM Success Rate | P50 | P95 |", "|---|---:|---:|---:|---:|---:|"])
        for dataset, summary in summaries.items():
            llm = summary["llm"]
            runtime = summary["runtime"]
            lines.append(
                f"| {dataset} | {llm['success']} | {llm['failed']} | {llm['success_rate']:.2%} | "
                f"{runtime['latency_ms_p50']:.0f}ms | {runtime['latency_ms_p95']:.0f}ms |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("rules", "hybrid"), required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    all_data = _load_all(ROOT)
    jobs = [(dataset, row) for dataset, rows in all_data.items() for row in rows]
    by_dataset: dict[str, list[dict[str, Any]]] = {dataset: [] for dataset in all_data}
    with ThreadPoolExecutor(max_workers=max(1, args.workers), thread_name_prefix=f"layer1-{args.mode}") as pool:
        futures = {pool.submit(_run_case, row, args.mode): (dataset, index) for index, (dataset, row) in enumerate(jobs)}
        indexed: list[tuple[str, int, dict[str, Any]]] = []
        for future in as_completed(futures):
            dataset, index = futures[future]
            indexed.append((dataset, index, future.result()))
    for dataset, _, row in sorted(indexed, key=lambda item: item[1]):
        by_dataset[dataset].append(row)
    summaries = {dataset: _summarize(rows, args.mode) for dataset, rows in by_dataset.items()}
    all_rows = [row for rows in by_dataset.values() for row in rows]
    all_summary = _summarize(all_rows, args.mode)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cases_path = args.output_dir / "cases.jsonl"
    failures_path = args.output_dir / "failures.jsonl"
    cases_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in all_rows), encoding="utf-8")
    failures_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in all_rows if row["failure_details"] or row["error"]),
        encoding="utf-8",
    )
    output = {
        "stage": "layer1",
        "mode": args.mode,
        "generated_at": datetime.now().astimezone().isoformat(),
        "workers": max(1, args.workers),
        "dataset_count": len(by_dataset),
        "datasets": summaries,
        "all": all_summary,
        "artifacts": {"cases": str(cases_path), "failures": str(failures_path)},
    }
    output_path = args.output_dir / "metrics.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "summary.md").write_text(_markdown(args.mode, summaries, output_path), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
