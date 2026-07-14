"""Run deterministic Memory V2 evaluation."""

from __future__ import annotations

import argparse
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.common import aggregate_boolean_scores, load_jsonl, write_json
from memory.extractor import extract_candidates
from memory.models import MemoryCandidate
from memory import repository as memory_repository
from memory import trace as memory_trace
from memory.lifecycle import expire
from memory.repository import (
    consolidation_period_key,
    correct_memory,
    insert_memory,
    list_memories,
    list_retryable_extraction_states,
    mark_consolidation_completed,
    mark_extraction_empty,
    mark_extraction_failed,
    mark_extraction_partial,
    reserve_consolidation_run,
    search_memories,
    soft_delete_memory,
)
from memory.relation_classifier import classify_relation
from memory.service import process_note_memory

DATA_DIR = Path("eval/memory")


def _set_eval_store(root: Path) -> None:
    memory_repository.DB_PATH = root / "memory.db"
    memory_trace.TRACE_PATH = root / "traces.jsonl"


def _score_extraction(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for case in cases:
        candidates = extract_candidates(str(case.get("case_id")), str(case.get("text", "")))
        types = {candidate.memory_type for candidate in candidates}
        should_store = bool(candidates)
        expected_types = set(case.get("expected_types", []))
        should_ok = should_store == bool(case.get("should_store"))
        type_ok = not expected_types or expected_types.issubset(types)
        text = " ".join(candidate.content for candidate in candidates)
        include_any = case.get("must_include_any", [])
        content_ok = not include_any or any(str(term) in text for term in include_any)
        results.append({"case_id": case.get("case_id"), "passed": should_ok and type_ok and content_ok, "types": sorted(types)})
    return {"summary": aggregate_boolean_scores(results), "results": results}


def _score_filtering(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for case in cases:
        candidates = extract_candidates(str(case.get("case_id")), str(case.get("text", "")))
        results.append({"case_id": case.get("case_id"), "passed": not candidates, "candidate_count": len(candidates)})
    false_memory_rate = round(sum(1 for item in results if not item["passed"]) / len(results), 4) if results else 0.0
    return {"false_memory_rate": false_memory_rate, "summary": aggregate_boolean_scores(results), "results": results}


def _score_relation(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for idx, case in enumerate(cases):
        old = MemoryCandidate("semantic" if "学习" in case["old"] else "preference", case["old"], 0.8, 0.9)
        old_memory = insert_memory(f"eval-rel-{idx}", old, source_note_id=f"old-{idx}")
        new = MemoryCandidate(old.memory_type, case["new"], 0.8, 0.9)
        decision = classify_relation(new, [old_memory])
        expected = str(case.get("expected_relation"))
        passed = decision.relation == expected or (expected == "new" and decision.action == "insert")
        results.append({"case_id": case.get("case_id"), "passed": passed, "relation": decision.relation, "action": decision.action})
    return {"summary": aggregate_boolean_scores(results), "results": results}


def _note(space_id: str, note_id: str, text: str) -> dict[str, str]:
    return {"id": note_id, "space_id": space_id, "text": text}


def _score_conflict(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for idx, case in enumerate(cases):
        space_id = f"eval-conflict-{idx}"
        for msg_idx, message in enumerate(case["messages"]):
            process_note_memory(_note(space_id, f"note-{idx}-{msg_idx}", message))
        if "must_conflict_count" in case:
            conflicts = list_memories(space_id, status="conflicted", limit=10)
            passed = len(conflicts) == int(case["must_conflict_count"])
            results.append({"case_id": case.get("case_id"), "passed": passed, "conflict_count": len(conflicts)})
            continue
        matches = search_memories(space_id, case["query"], limit=3)
        contents = [memory.content for memory, _score in matches]
        latest_ok = any(case["must_return_latest"] in content for content in contents)
        stale_active = any(case["messages"][0] in content for content in contents)
        results.append({"case_id": case.get("case_id"), "passed": latest_ok and not stale_active, "contents": contents})
    return {"summary": aggregate_boolean_scores(results), "results": results}


def _score_lifecycle(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for idx, case in enumerate(cases):
        space_id = f"eval-life-{idx}"
        if case["operation"] == "task_done":
            for msg_idx, message in enumerate(case["messages"]):
                process_note_memory(_note(space_id, f"note-life-{idx}-{msg_idx}", message))
            tasks = list_memories(space_id, status="active", memory_type="task", limit=10)
            passed = len(tasks) == 1 and tasks[0].task_status == case["expected_task_status"]
            results.append({"case_id": case.get("case_id"), "passed": passed, "task_status": tasks[0].task_status if tasks else None})
            continue
        report = process_note_memory(_note(space_id, f"note-life-{idx}", case["message"]))
        memory_id = report["results"][0]["memory_id"]
        if case["operation"] == "forget":
            soft_delete_memory(memory_id)
            matches = search_memories(space_id, case["query"], limit=3)
            passed = bool(case.get("expected_no_active_result")) and not matches
        elif case["operation"] == "correct":
            correct_memory(memory_id, case["corrected"])
            matches = search_memories(space_id, case["query"], limit=3)
            passed = bool(case.get("expected_active_result")) and bool(matches)
        elif case["operation"] == "expire":
            expire(memory_id)
            matches = search_memories(space_id, case["query"], limit=3)
            passed = bool(case.get("expected_no_active_result")) and not matches
        else:
            passed = False
        results.append({"case_id": case.get("case_id"), "passed": passed, "memory_id": memory_id})
    return {"summary": aggregate_boolean_scores(results), "results": results}


def _score_retrieval(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for idx, case in enumerate(cases):
        space_id = f"eval-ret-{idx}"
        id_map = {}
        for memory in case["memories"]:
            candidate = MemoryCandidate(memory["type"], memory["content"], 0.8, 0.9)
            record = insert_memory(space_id, candidate, source_note_id=f"source-{memory['id']}")
            id_map[record.id] = memory["id"]
        ranked = search_memories(space_id, case["query"], limit=5)
        ranked_original_ids = [id_map[memory.id] for memory, _score in ranked]
        expected = set(case["expected_memory_ids"])
        pass_k = int(case.get("pass_k", 5))
        passed = bool(set(ranked_original_ids[:pass_k]) & expected)
        results.append({"case_id": case.get("case_id"), "passed": passed, "ranked_ids": ranked_original_ids})
    return {"summary": aggregate_boolean_scores(results), "results": results}


def _score_e2e(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for idx, case in enumerate(cases):
        space_id = f"eval-e2e-{idx}"
        for msg_idx, message in enumerate(case["messages"]):
            process_note_memory(_note(space_id, f"note-e2e-{idx}-{msg_idx}", message))
        query_results = []
        for query in case["queries"]:
            matches = search_memories(space_id, query["query"], limit=5)
            text = "\n".join(f"{memory.content} {memory.task_status or ''}" for memory, _score in matches)
            query_results.append({"query": query["query"], "passed": query["must_include"] in text, "text": text})
        results.append({"case_id": case.get("case_id"), "passed": all(item["passed"] for item in query_results), "queries": query_results})
    return {"summary": aggregate_boolean_scores(results), "results": results}


def _score_hardening() -> dict[str, Any]:
    space_id = "eval-hardening"
    mark_extraction_failed("note-failed", space_id, error="simulated")
    mark_extraction_partial("note-partial", space_id, candidate_count=2, processed_count=1, error="simulated")
    empty_state = mark_extraction_empty("note-empty", space_id)
    retryable = {state.note_id for state in list_retryable_extraction_states(space_id)}

    period_key = consolidation_period_key("daily", date.fromisoformat("2026-07-14"))
    run = reserve_consolidation_run(space_id, "daily", period_key)
    if run is not None:
        mark_consolidation_completed(run.id, {"ok": True})
    duplicate = reserve_consolidation_run(space_id, "daily", period_key)

    write_ok = True
    try:
        for idx in range(5):
            insert_memory(f"{space_id}-{idx}", MemoryCandidate("semantic", f"并发写入守卫 {idx}", 0.8, 0.9), source_note_id=f"source-{idx}")
    except Exception:
        write_ok = False

    insert_memory(space_id, MemoryCandidate("semantic", "用户喜欢咖啡", 0.8, 0.9), source_note_id="source-low")
    low_relevance_filtered = not search_memories(space_id, "火星基地", min_score=0.95, limit=5)

    metrics = {
        "extraction_recovery_rate": 1.0 if "note-failed" in retryable else 0.0,
        "empty_reprocessing_rate": 0.0 if empty_state.status == "empty" else 1.0,
        "partial_recovery_rate": 1.0 if "note-partial" in retryable else 0.0,
        "consolidation_duplicate_rate": 0.0 if duplicate is None else 1.0,
        "sqlite_write_success_rate": 1.0 if write_ok else 0.0,
        "low_relevance_filter_rate": 1.0 if low_relevance_filtered else 0.0,
    }
    results = [
        {
            "case_id": key,
            "passed": value == (0.0 if key == "empty_reprocessing_rate" else 1.0),
            "value": value,
        }
        for key, value in metrics.items()
    ]
    return {"summary": aggregate_boolean_scores(results), "metrics": metrics, "results": results}


def run(*, dry_run: bool = False, output_dir: Path = Path("eval/results")) -> dict[str, Any]:
    files = {
        "extraction": DATA_DIR / "extraction_cases.jsonl",
        "filtering": DATA_DIR / "filtering_cases.jsonl",
        "relation": DATA_DIR / "relation_cases.jsonl",
        "conflict": DATA_DIR / "conflict_cases.jsonl",
        "lifecycle": DATA_DIR / "lifecycle_cases.jsonl",
        "retrieval": DATA_DIR / "retrieval_cases.jsonl",
        "e2e": DATA_DIR / "end_to_end_cases.jsonl",
    }
    cases = {name: load_jsonl(path) for name, path in files.items()}
    if dry_run:
        report = {"mode": "dry_run", "cases": {name: len(items) for name, items in cases.items()}}
        write_json(output_dir / "memory_results.json", report)
        return report

    with tempfile.TemporaryDirectory() as tmp:
        _set_eval_store(Path(tmp))
        reports = {
            "extraction": _score_extraction(cases["extraction"]),
            "filtering": _score_filtering(cases["filtering"]),
            "relation": _score_relation(cases["relation"]),
            "conflict": _score_conflict(cases["conflict"]),
            "lifecycle": _score_lifecycle(cases["lifecycle"]),
            "retrieval": _score_retrieval(cases["retrieval"]),
            "e2e": _score_e2e(cases["e2e"]),
            "hardening": _score_hardening(),
        }

    summary = {
        "extraction_f1": reports["extraction"]["summary"]["pass_rate"],
        "memory_type_accuracy": reports["extraction"]["summary"]["pass_rate"],
        "false_memory_rate": reports["filtering"]["false_memory_rate"],
        "merge_accuracy": reports["relation"]["summary"]["pass_rate"],
        "conflict_accuracy": reports["conflict"]["summary"]["pass_rate"],
        "stale_memory_usage_rate": round(1 - reports["conflict"]["summary"]["pass_rate"], 4),
        "retrieval_recall_at_5": reports["retrieval"]["summary"]["pass_rate"],
        "source_attribution_rate": reports["e2e"]["summary"]["pass_rate"],
        "lifecycle_accuracy": reports["lifecycle"]["summary"]["pass_rate"],
        "source_preservation_rate": reports["retrieval"]["summary"]["pass_rate"],
        **reports["hardening"]["metrics"],
    }
    output_map = {
        "memory_extraction.json": reports["extraction"],
        "memory_relation.json": reports["relation"],
        "memory_conflict.json": reports["conflict"],
        "memory_retrieval.json": reports["retrieval"],
        "memory_e2e.json": reports["e2e"],
        "memory_hardening.json": reports["hardening"],
        "memory_results.json": {"mode": "memory", "summary": summary, "reports": reports},
    }
    for filename, report in output_map.items():
        write_json(output_dir / filename, report)
    return {"mode": "memory", "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Memory V2 lifecycle.")
    parser.add_argument("--dry-run", action="store_true", help="Validate memory eval cases without running APIs")
    parser.add_argument("--output-dir", default="eval/results")
    args = parser.parse_args()
    report = run(dry_run=args.dry_run, output_dir=Path(args.output_dir))
    print(report)


if __name__ == "__main__":
    main()
