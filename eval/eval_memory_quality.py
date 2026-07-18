"""Evaluate the 360-case Memory V2 quality set without external model calls."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The quality set is intentionally offline.  Keep it independent from the
# deployment's PostgreSQL/Redis settings and from agent observability hooks.
os.environ["STORAGE_BACKEND"] = "local"
os.environ["COORDINATION_BACKEND"] = "local"
os.environ["TASK_QUEUE_BACKEND"] = "local"
os.environ["SUIXINJI_AGENT_HOOKS_ENABLED"] = "false"

from eval.common import load_jsonl, write_json
from memory import repository as memory_repository
from memory import trace as memory_trace
from memory.adjudicator import adjudicate_memory
from memory.extractor import extract_candidates
from memory.models import MemoryCandidate
from memory.policies.preference import preference_signature
from memory.repository import insert_memory, search_memories
from memory.service import _process_note_memory_impl


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval" / "memory" / "quality_cases.jsonl"
DESTRUCTIVE_RELATIONS = {"merge", "update_task", "supersede", "conflict"}
RELATIONS = ("new", "same", "merge", "update_task", "supersede", "conflict")


def _f1(precision_counts: tuple[int, int, int]) -> dict[str, float]:
    true_positive, false_positive, false_negative = precision_counts
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
    }


def _extract_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    type_counts: Counter[str] = Counter()
    results: list[dict[str, Any]] = []
    exact_store = 0
    for case in cases:
        predicted = extract_candidates(str(case["case_id"]), str(case["text"]))
        predicted_types = {candidate.memory_type for candidate in predicted}
        expected_types = {str(item) for item in case.get("expected_types", [])}
        for memory_type in {"episodic", "semantic", "preference", "task"}:
            type_counts[f"{memory_type}:tp"] += int(memory_type in predicted_types and memory_type in expected_types)
            type_counts[f"{memory_type}:fp"] += int(memory_type in predicted_types and memory_type not in expected_types)
            type_counts[f"{memory_type}:fn"] += int(memory_type not in predicted_types and memory_type in expected_types)
        store_ok = bool(predicted) == bool(case.get("should_store"))
        exact_store += int(store_ok)
        results.append(
            {
                "case_id": case["case_id"],
                "category": case["category"],
                "predicted_types": sorted(predicted_types),
                "expected_types": sorted(expected_types),
                "candidate_count": len(predicted),
                "store_ok": store_ok,
                "type_exact": predicted_types == expected_types,
            }
        )
    per_type = {
        memory_type: _f1(
            (
                type_counts[f"{memory_type}:tp"],
                type_counts[f"{memory_type}:fp"],
                type_counts[f"{memory_type}:fn"],
            )
        )
        for memory_type in ("episodic", "semantic", "preference", "task")
    }
    return {
        "cases": len(cases),
        "exact_store_rate": round(exact_store / len(cases), 4) if cases else 0.0,
        "type_macro_f1": round(sum(item["f1"] for item in per_type.values()) / len(per_type), 4),
        "per_type": per_type,
        "results": results,
    }


def _task_status(text: str, *, old: bool = False) -> str | None:
    if "记得" in text or "待办" in text:
        return "todo"
    if "完成" in text or "已解决" in text:
        return "done"
    if "阻塞" in text:
        return "blocked"
    if "进行" in text or "开始" in text or "继续" in text:
        return "in_progress"
    return "todo"


def _candidate(text: str, memory_type: str, note_id: str, *, old: bool = False) -> MemoryCandidate:
    signature = preference_signature(text) if memory_type == "preference" else None
    predicate = None
    if memory_type == "semantic":
        predicate = "location" if any(marker in text for marker in ("住在", "搬到")) else "learning_focus"
    elif memory_type == "preference":
        predicate = "preference"
    elif memory_type == "task":
        predicate = "task"
    return MemoryCandidate(
        memory_type=memory_type,
        content=text,
        importance=0.8,
        confidence=0.9,
        note_id=note_id,
        subject="user",
        predicate=predicate,
        object_value=signature.topic if signature else None,
        task_status=_task_status(text, old=old),
    )


def _relation_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    predictions: list[str] = []
    expected: list[str] = []
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "relations.db"
        for case in cases:
            case_id = str(case["case_id"])
            old = _candidate(str(case["old"]), str(case["old_type"]), f"{case_id}-old", old=True)
            old_memory = insert_memory(case_id, old, source_note_id=f"{case_id}-old", db_path=db_path)
            new = _candidate(str(case["new"]), str(case["new_type"]), f"{case_id}-new")
            decision = adjudicate_memory(new, [old_memory])
            predictions.append(decision.relation)
            expected_relation = str(case["expected_relation"])
            expected.append(expected_relation)
            results.append(
                {
                    "case_id": case_id,
                    "category": case["category"],
                    "expected": expected_relation,
                    "predicted": decision.relation,
                    "action": decision.recommended_action,
                    "confidence": decision.confidence,
                }
            )
    confusion = {label: {other: 0 for other in RELATIONS} for label in RELATIONS}
    for actual, prediction in zip(expected, predictions):
        confusion.setdefault(actual, {}).setdefault(prediction, 0)
        confusion[actual][prediction] += 1
    per_relation: dict[str, dict[str, float]] = {}
    for label in RELATIONS:
        tp = sum(actual == label and prediction == label for actual, prediction in zip(expected, predictions))
        fp = sum(actual != label and prediction == label for actual, prediction in zip(expected, predictions))
        fn = sum(actual == label and prediction != label for actual, prediction in zip(expected, predictions))
        per_relation[label] = _f1((tp, fp, fn))
    false_destructive = sum(prediction in DESTRUCTIVE_RELATIONS and actual not in DESTRUCTIVE_RELATIONS for actual, prediction in zip(expected, predictions))
    destructive_total = sum(actual not in DESTRUCTIVE_RELATIONS for actual in expected)
    return {
        "cases": len(cases),
        "accuracy": round(sum(actual == prediction for actual, prediction in zip(expected, predictions)) / len(cases), 4),
        "macro_f1": round(sum(item["f1"] for item in per_relation.values()) / len(per_relation), 4),
        "per_relation": per_relation,
        "false_destructive_rate": round(false_destructive / destructive_total, 4) if destructive_total else 0.0,
        "confusion_matrix": confusion,
        "results": results,
    }


def _retrieval_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "retrieval.db"
        for case in cases:
            case_id = str(case["case_id"])
            target = case["target"]
            memories = [target, *case["distractors"]]
            for item in memories:
                candidate = _candidate(str(item["content"]), str(item["type"]), f"{case_id}-{item['id']}")
                insert_memory(case_id, candidate, source_note_id=f"source-{item['id']}", db_path=db_path)
            ranked = search_memories(case_id, str(case["query"]), limit=10, mark_access=False, db_path=db_path)
            ranked_ids = [memory.sources[0].note_id.removeprefix("source-") for memory, _score in ranked if memory.sources]
            target_id = str(target["id"])
            rank = ranked_ids.index(target_id) + 1 if target_id in ranked_ids else None
            recall = 1.0 if rank is not None and rank <= 20 else 0.0
            recalls.append(recall)
            reciprocal_ranks.append(1 / rank if rank else 0.0)
            results.append({"case_id": case_id, "ranked_ids": ranked_ids, "target_id": target_id, "rank": rank})
    return {
        "cases": len(cases),
        "recall_at_20": round(sum(recalls) / len(recalls), 4) if recalls else 0.0,
        "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4) if reciprocal_ranks else 0.0,
        "results": results,
    }


def _e2e_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    old_db = memory_repository.DB_PATH
    old_trace = memory_trace.TRACE_PATH
    with tempfile.TemporaryDirectory() as tmp:
        memory_repository.DB_PATH = Path(tmp) / "e2e.db"
        memory_trace.TRACE_PATH = Path(tmp) / "traces.jsonl"
        try:
            for case in cases:
                space_id = f"quality-{case['case_id']}"
                for index, message in enumerate(case["messages"]):
                    _process_note_memory_impl({"id": f"{case['case_id']}-note-{index}", "space_id": space_id, "text": message})
                matches = search_memories(space_id, str(case["query"]), limit=5, mark_access=False)
                contents = [memory.content for memory, _score in matches]
                passed = not contents if case.get("expected_no_result") else any(str(case["must_include"]) in content for content in contents)
                results.append(
                    {
                        "case_id": case["case_id"],
                        "category": case["category"],
                        "passed": passed,
                        "expected_no_result": bool(case.get("expected_no_result")),
                        "query": case["query"],
                        "contents": contents,
                    }
                )
        finally:
            memory_repository.DB_PATH = old_db
            memory_trace.TRACE_PATH = old_trace
    return {
        "cases": len(cases),
        "accuracy": round(sum(bool(item["passed"]) for item in results) / len(results), 4) if results else 0.0,
        "results": results,
    }


def _routing_plan() -> dict[str, Any]:
    return {
        "baseline_external_calls": False,
        "fast_model": os.getenv("SUIXINJI_FAST_MODEL", "gpt-5.4-mini"),
        "balanced_model": os.getenv("SUIXINJI_BALANCED_MODEL", "gpt-5.4"),
        "strong_model": os.getenv("SUIXINJI_STRONG_MODEL", "gpt-5.5"),
        "recommended_roles": {
            "rule_prefilter_and_structured_extraction": "fast_model",
            "low_risk_candidate_validation": "fast_model",
            "high_risk_relation_adjudication": "strong_model",
            "complex_query_synthesis": "balanced_model",
            "embedding": os.getenv("EMBEDDING_MODEL", "configured_embedding_model"),
        },
        "note": "Model routing is recorded for later stages; this baseline intentionally uses deterministic rules.",
    }


def run(*, output: Path) -> dict[str, Any]:
    cases = load_jsonl(DATASET)
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[str(case["kind"])].append(case)
    report = {
        "schema_version": "memory-quality-v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "mode": "deterministic_rules",
        "dataset": {"path": str(DATASET.relative_to(ROOT)), "cases": len(cases), "by_kind": {key: len(value) for key, value in sorted(grouped.items())}},
        "routing": _routing_plan(),
        "extraction": _extract_report(grouped["extraction"]),
        "relation": _relation_report(grouped["relation"]),
        "retrieval": _retrieval_report(grouped["retrieval"]),
        "end_to_end": _e2e_report(grouped["e2e"]),
    }
    write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the offline Memory quality baseline")
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "memory_eval" / "baseline.json")
    args = parser.parse_args()
    report = run(output=args.output)
    print({key: report[key] for key in ("commit", "dataset", "extraction", "relation", "retrieval", "end_to_end")})


if __name__ == "__main__":
    main()
