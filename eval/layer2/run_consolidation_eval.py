"""Run deterministic Layer 2 evaluation against the real consolidator."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from .adapter import Stage2EvalAdapter
    from .metrics import evaluate
    from .validate_dataset import EXPECTED_FILES
except ImportError:  # direct `python eval/layer2/run_consolidation_eval.py` invocation
    from eval.layer2.adapter import Stage2EvalAdapter
    from eval.layer2.metrics import evaluate
    from eval.layer2.validate_dataset import EXPECTED_FILES


def _load(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[:limit] if limit else rows


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _run_cases(rows: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    results = []
    with tempfile.TemporaryDirectory(prefix=f"layer2-{run_id}-") as temp_dir:
        for index, case in enumerate(rows):
            adapter = Stage2EvalAdapter(case, Path(temp_dir) / f"case_{index}.db", run_id)
            result = adapter.run()
            result["input"] = case["input"]
            result["coverage_tags"] = case.get("coverage_tags", [])
            result["difficulty"] = case.get("difficulty")
            result["predicted_decisions"] = [
                {
                    **decision,
                    "raw_result": {},
                }
                for decision in result["predicted_decisions"]
            ]
            results.append(result)
    return results


def _write_report(output_dir: Path, dataset_name: str, results: list[dict[str, Any]], source_zip: Path | None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = evaluate(results)
    predictions = output_dir / "layer2_predictions.jsonl"
    failures = output_dir / "layer2_failed_cases.jsonl"
    with predictions.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    with failures.open("w", encoding="utf-8") as handle:
        for result in results:
            if not result.get("case_exact"):
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    (output_dir / "layer2_relation_confusion.json").write_text(json.dumps(summary["relation_confusion"], ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "layer2_action_confusion.json").write_text(json.dumps(summary["action_confusion"], ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "layer2_transition_breakdown.json").write_text(json.dumps({"task_transition_accuracy": summary["task_transition_accuracy"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "layer2_orphan_done_report.json").write_text(json.dumps({key: summary[key] for key in ("orphan_done_cases", "orphan_done_task_rate", "conversion_to_episodic_accuracy")}, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "layer2_version_source_report.json").write_text(json.dumps({key: summary[key] for key in ("current_state_field_accuracy", "source_link", "duplicate_active_rate", "stale_active_rate")}, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "layer2_metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    source_hash = None
    if source_zip and source_zip.exists():
        source_hash = hashlib.sha256(source_zip.read_bytes()).hexdigest()
    manifest = {
        "run_id": output_dir.name,
        "generated_at": datetime.now().astimezone().isoformat(),
        "git_commit": _git_sha(),
        "python": platform.python_version(),
        "dataset": dataset_name,
        "dataset_sha256": source_hash,
        "backend": "isolated sqlite",
        "mode": "deterministic",
        "first_stage_extractor_called": False,
        "production_space_written": False,
        "case_count": len(results),
    }
    (output_dir / "layer2_run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        f"# Layer 2 consolidation report — {dataset_name}",
        "",
        f"- Cases: {len(results)}",
        "- 输入：validated MemoryCandidate",
        "- 第一阶段抽取器：未调用",
        "- 数据库：每个 Case 独立临时 SQLite",
        "- 生产 Space：未写入",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Task Identity F1 | {summary['task_identity']['f1'] * 100:.2f}% |",
        f"| Relation Macro-F1 | {summary['relation_macro_f1'] * 100:.2f}% |",
        f"| Action Accuracy | {summary['action_accuracy'] * 100:.2f}% |",
        f"| Task Transition Accuracy | {summary['task_transition_accuracy'] * 100:.2f}% |",
        f"| Version Sequence Accuracy | {summary['version_sequence_accuracy'] * 100:.2f}% |",
        f"| Version Creation Accuracy | {summary['version_creation_accuracy'] * 100:.2f}% |",
        f"| Idempotence Accuracy | {summary['idempotence_accuracy'] * 100:.2f}% |",
        f"| Case Exact Match | {summary['case_exact_match'] * 100:.2f}% |",
        f"| Pending-review F1 | {summary['pending_review']['f1'] * 100:.2f}% |",
        f"| Source Link F1 | {summary['source_link']['f1'] * 100:.2f}% |",
        f"| Duplicate Active Rate | {summary['duplicate_active_rate'] * 100:.2f}% |",
        f"| Stale Active Rate | {summary['stale_active_rate'] * 100:.2f}% |",
        f"| Orphan Done Task Rate | {summary['orphan_done_task_rate'] * 100:.2f}% |",
        f"| Conversion-to-Episodic Accuracy | {summary['conversion_to_episodic_accuracy'] * 100:.2f}% |",
        "",
        f"失败 Case：{summary['failure_count']}，详情见 `layer2_failed_cases.jsonl`。",
    ]
    (output_dir / "layer2_summary.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--data", type=Path)
    group.add_argument("--data-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "eval/results/layer2")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--run-id", default=datetime.now().astimezone().strftime("layer2_%Y%m%d_%H%M%S"))
    parser.add_argument("--backend", default="sqlite")
    parser.add_argument("--source-zip", type=Path)
    args = parser.parse_args()
    del args.backend
    if args.data:
        paths = [args.data]
    else:
        paths = [args.data_dir / name for name in EXPECTED_FILES]
    all_rows: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    for path in paths:
        rows = _load(path, args.limit)
        dataset_name = path.stem
        results = _run_cases(rows, f"{args.run_id}_{dataset_name}")
        summary = _write_report(args.output_dir / dataset_name, dataset_name, results, args.source_zip)
        summaries[dataset_name] = summary
        all_rows.extend(results)
        if args.fail_fast and summary["failure_count"]:
            break
    if len(paths) > 1:
        combined = _write_report(args.output_dir / "all", "all_datasets", all_rows, args.source_zip)
        (args.output_dir / "layer2_all_metrics.json").write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output_dir / "layer2_dataset_summaries.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(args.output_dir), "datasets": summaries}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
