"""Run offline ReAct query evaluation with real LLM routing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import query_agent
from core.llm_client import embed_text
from eval.common import aggregate_boolean_scores, load_jsonl, score_query_react, write_json
from eval.eval_retrieval import note_search_text
from storage.vector_store import cosine_similarity


def _clip(text: str | None, limit: int = 500) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def local_semantic_search(notes: list[dict[str, Any]], query: str, top_k: int, min_score: float) -> list[dict[str, Any]]:
    query_embedding = embed_text(query)
    ranked = []
    for note in notes:
        embedding = embed_text(note_search_text(note))
        score = cosine_similarity(query_embedding, embedding)
        if score < min_score:
            continue
        ranked.append((score, note))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "id": note.get("id"),
            "message_id": note.get("message_id"),
            "score": round(score, 4),
            "title": note.get("title"),
            "type": note.get("type"),
            "tags": note.get("tags", []),
            "summary": note.get("summary"),
            "time": note.get("ts") or note.get("time"),
            "text": _clip(note.get("text")),
        }
        for score, note in ranked[:top_k]
    ]


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    notes = list(case.get("notes", []))
    tool_calls: list[dict[str, Any]] = []

    original_load_index = query_agent.load_index
    original_semantic_search = query_agent.semantic_search
    original_run_tool = query_agent._run_tool

    def fake_load_index(space_id: str) -> list[dict[str, Any]]:
        return notes

    def fake_semantic_search(space_id: str, query: str, top_k: int = 5, min_score: float = query_agent.DEFAULT_QUERY_MIN_SCORE):
        return local_semantic_search(notes, query, top_k, min_score)

    def recording_run_tool(space_id: str, action: str, args: dict[str, Any]) -> Any:
        result = original_run_tool(space_id, action, args)
        tool_calls.append({"tool": action, "args": args, "result": result})
        return result

    try:
        query_agent.load_index = fake_load_index
        query_agent.semantic_search = fake_semantic_search
        query_agent._run_tool = recording_run_tool
        answer = query_agent.answer_question(
            "eval_space",
            str(case.get("question", "")),
            max_steps=int(case.get("max_steps", 4)),
        )
    finally:
        query_agent.load_index = original_load_index
        query_agent.semantic_search = original_semantic_search
        query_agent._run_tool = original_run_tool

    score = score_query_react(tool_calls, answer, case)
    score["question"] = case.get("question")
    return score


def run(cases_path: Path, *, dry_run: bool = False, max_cases: int | None = None) -> dict[str, object]:
    cases = load_jsonl(cases_path)
    if max_cases is not None:
        cases = cases[:max_cases]

    if dry_run:
        return {"mode": "dry_run", "cases": len(cases), "case_ids": [case.get("case_id") for case in cases]}

    results = [run_case(case) for case in cases]
    return {
        "mode": "query_react",
        "summary": aggregate_boolean_scores(results),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate complete ReAct query behavior.")
    parser.add_argument("--cases", default="eval/data/query_cases.jsonl")
    parser.add_argument("--output", default="eval/results/query_react_results.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate cases without calling LLM/embedding API")
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()

    report = run(Path(args.cases), dry_run=args.dry_run, max_cases=args.max_cases)
    write_json(args.output, report)
    print(f"Wrote {args.output}")
    print(report.get("summary", report))


if __name__ == "__main__":
    main()
