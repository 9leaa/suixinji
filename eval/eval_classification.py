"""Run offline classification evaluation with the real classifier."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.classifier import classify_text
from eval.common import aggregate_boolean_scores, load_jsonl, score_classification, write_json


def run(cases_path: Path, *, dry_run: bool = False, max_cases: int | None = None) -> dict[str, object]:
    cases = load_jsonl(cases_path)
    if max_cases is not None:
        cases = cases[:max_cases]

    if dry_run:
        return {"mode": "dry_run", "cases": len(cases), "case_ids": [case.get("case_id") for case in cases]}

    results = []
    for case in cases:
        prediction = classify_text(str(case.get("text", "")))
        results.append(score_classification(prediction, case))

    return {
        "mode": "classification",
        "summary": aggregate_boolean_scores(results),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate note classification quality.")
    parser.add_argument("--cases", default="eval/data/classification_cases.jsonl")
    parser.add_argument("--output", default="eval/results/classification_results.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate cases without calling LLM")
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()

    report = run(Path(args.cases), dry_run=args.dry_run, max_cases=args.max_cases)
    write_json(args.output, report)
    print(f"Wrote {args.output}")
    print(report.get("summary", report))


if __name__ == "__main__":
    main()
