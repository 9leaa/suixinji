"""Run offline summary quality evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.llm_client import complete_json
from eval.common import aggregate_boolean_scores, load_jsonl, score_summary, write_json
from summary.daily_summary import REFLECTION_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT


def _stats(notes: list[dict[str, Any]]) -> dict[str, Any]:
    type_counter = Counter(str(note.get("type") or "未分类") for note in notes)
    tag_counter: Counter[str] = Counter()
    for note in notes:
        tag_counter.update(str(tag) for tag in note.get("tags", []))
    return {
        "note_count": len(notes),
        "type_counts": dict(type_counter.most_common()),
        "top_tags": dict(tag_counter.most_common(20)),
    }


def generate_case_summary(case: dict[str, Any]) -> str:
    notes = list(case.get("notes", []))
    payload = {
        "range_label": case.get("range_label", "评测范围"),
        "start": case.get("start"),
        "end": case.get("end"),
        "stats": _stats(notes),
        "notes": notes,
    }
    draft = complete_json(
        system_prompt=SUMMARY_SYSTEM_PROMPT,
        user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
    ).get("summary_markdown", "")
    reviewed = complete_json(
        system_prompt=REFLECTION_SYSTEM_PROMPT,
        user_prompt=json.dumps({"notes": notes, "draft": draft}, ensure_ascii=False, indent=2),
    ).get("final_summary", "")
    return str(reviewed or draft).strip()


def run(cases_path: Path, *, dry_run: bool = False, max_cases: int | None = None) -> dict[str, object]:
    cases = load_jsonl(cases_path)
    if max_cases is not None:
        cases = cases[:max_cases]

    if dry_run:
        return {"mode": "dry_run", "cases": len(cases), "case_ids": [case.get("case_id") for case in cases]}

    results = []
    for case in cases:
        summary = generate_case_summary(case)
        score = score_summary(summary, case)
        score["summary"] = summary
        results.append(score)

    return {
        "mode": "summary",
        "summary": aggregate_boolean_scores(results),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate summary coverage quality.")
    parser.add_argument("--cases", default="eval/data/summary_cases.jsonl")
    parser.add_argument("--output", default="eval/results/summary_results.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate cases without calling LLM")
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()

    report = run(Path(args.cases), dry_run=args.dry_run, max_cases=args.max_cases)
    write_json(args.output, report)
    print(f"Wrote {args.output}")
    print(report.get("summary", report))


if __name__ == "__main__":
    main()
