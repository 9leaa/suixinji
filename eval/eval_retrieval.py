"""Run offline embedding retrieval evaluation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.llm_client import embed_text
from eval.common import aggregate_boolean_scores, load_jsonl, score_retrieval, write_json
from storage.vector_store import cosine_similarity


def note_search_text(note: dict[str, Any]) -> str:
    tags = " ".join(str(tag) for tag in note.get("tags", []))
    related = " ".join(str(item) for item in note.get("related", []))
    return "\n".join(
        part
        for part in [
            str(note.get("title") or ""),
            str(note.get("type") or ""),
            tags,
            str(note.get("summary") or ""),
            str(note.get("text") or ""),
            related,
        ]
        if part
    )


def rank_notes(query: str, notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    query_embedding = embed_text(query)
    ranked = []
    for note in notes:
        text = note_search_text(note)
        embedding = embed_text(text)
        ranked.append(
            {
                "note_id": str(note.get("id")),
                "score": cosine_similarity(query_embedding, embedding),
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def run(cases_path: Path, *, dry_run: bool = False, max_cases: int | None = None) -> dict[str, object]:
    cases = load_jsonl(cases_path)
    if max_cases is not None:
        cases = cases[:max_cases]

    if dry_run:
        return {"mode": "dry_run", "cases": len(cases), "case_ids": [case.get("case_id") for case in cases]}

    results = []
    for case in cases:
        ranked = rank_notes(str(case.get("query", "")), list(case.get("notes", [])))
        ranked_ids = [item["note_id"] for item in ranked]
        scores_by_id = {item["note_id"]: float(item["score"]) for item in ranked}
        score = score_retrieval(ranked_ids, case, scores_by_id=scores_by_id)
        score["ranked"] = ranked
        results.append(score)

    return {
        "mode": "retrieval",
        "summary": aggregate_boolean_scores(results),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate embedding retrieval quality.")
    parser.add_argument("--cases", default="eval/data/retrieval_cases.jsonl")
    parser.add_argument("--output", default="eval/results/retrieval_results.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate cases without calling embedding API")
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()

    report = run(Path(args.cases), dry_run=args.dry_run, max_cases=args.max_cases)
    write_json(args.output, report)
    print(f"Wrote {args.output}")
    print(report.get("summary", report))


if __name__ == "__main__":
    main()
