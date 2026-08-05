"""Retry only final Hybrid transport failures and overwrite them in the final artifact.

Intermediate failed attempts are intentionally not written.  The result directory
contains only the final candidate output (or the final error if every retry fails).
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path("/home/zcj/suixinji")
sys.path.insert(0, str(ROOT))
from eval.layer1.run_full_benchmark import _load_all, _markdown, _run_case, _summarize  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()
    cases_path = args.output_dir / "cases.jsonl"
    rows = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    fixtures = {str(row["case_id"]): row for dataset in _load_all(ROOT).values() for row in dataset}
    targets = [(index, row) for index, row in enumerate(rows) if row.get("error") is not None]

    def retry(index: int, old: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        fixture = fixtures[str(old["case_id"])]
        final = old
        # No individual attempt log is persisted; only the eventual output replaces old.
        for _ in range(max(1, args.max_attempts)):
            candidate = _run_case(fixture, "hybrid")
            final = candidate
            if candidate.get("error") is None:
                break
        return index, final

    with ThreadPoolExecutor(max_workers=max(1, args.workers), thread_name_prefix="layer1-hybrid-retry") as pool:
        futures = [pool.submit(retry, index, row) for index, row in targets]
        for future in as_completed(futures):
            index, result = future.result()
            rows[index] = result

    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_dataset.setdefault(str(row["dataset"]), []).append(row)
    summaries = {dataset: _summarize(dataset_rows, "hybrid") for dataset, dataset_rows in sorted(by_dataset.items())}
    all_summary = _summarize(rows, "hybrid")
    cases_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (args.output_dir / "failures.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows if row["failure_details"] or row["error"]), encoding="utf-8")
    metrics = {"stage": "layer1", "mode": "hybrid", "generated_at": datetime.now().astimezone().isoformat(), "workers": args.workers, "dataset_count": len(summaries), "datasets": summaries, "all": all_summary}
    (args.output_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "summary.md").write_text(_markdown("hybrid", summaries, args.output_dir / "metrics.json"), encoding="utf-8")
    print(json.dumps({"retried_cases": len(targets), "final_llm": all_summary["llm"], "output_dir": str(args.output_dir)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
