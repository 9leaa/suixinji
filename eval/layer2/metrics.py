"""Metrics for deterministic Layer 2 consolidation evaluation."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .mappings import EVAL_ACTIONS, EVAL_RELATIONS


def _task_status(value: Any) -> str | None:
    status = str(value or "").casefold() or None
    return {"in_progress": "todo", "blocked": "todo", "cancelled": "done", "canceled": "done"}.get(status, status)


def _f1(tp: int, fp: int, fn: int) -> float:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _prf(gold: list[str], predicted: list[str]) -> dict[str, float]:
    expected = Counter(gold)
    actual = Counter(predicted)
    tp = sum(min(expected[key], actual[key]) for key in expected.keys() | actual.keys())
    fp = sum(max(0, actual[key] - expected[key]) for key in expected.keys() | actual.keys())
    fn = sum(max(0, expected[key] - actual[key]) for key in expected.keys() | actual.keys())
    return {"precision": tp / (tp + fp) if tp + fp else 0.0, "recall": tp / (tp + fn) if tp + fn else 0.0, "f1": _f1(tp, fp, fn), "tp": tp, "fp": fp, "fn": fn}


def _binary_prf(gold: list[bool], predicted: list[bool]) -> dict[str, float]:
    """Positive-class metrics for safety gates such as pending review."""
    tp = sum(expected and actual for expected, actual in zip(gold, predicted))
    fp = sum(not expected and actual for expected, actual in zip(gold, predicted))
    fn = sum(expected and not actual for expected, actual in zip(gold, predicted))
    tn = sum(not expected and not actual for expected, actual in zip(gold, predicted))
    return {
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "f1": _f1(tp, fp, fn),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def _confusion(gold: list[str], predicted: list[str], labels: tuple[str, ...]) -> dict[str, dict[str, int]]:
    matrix = {label: {other: 0 for other in (*labels, "other")} for label in (*labels, "other")}
    for expected, actual in zip(gold, predicted):
        expected_label = expected if expected in labels else "other"
        actual_label = actual if actual in labels else "other"
        matrix[expected_label][actual_label] += 1
    return matrix


def _per_label_scores(gold: list[str], predicted: list[str], labels: tuple[str, ...]) -> dict[str, dict[str, float]]:
    """One-vs-rest scores; do not filter both sides to the same label."""
    scores: dict[str, dict[str, float]] = {}
    for label in labels:
        tp = sum(expected == label and actual == label for expected, actual in zip(gold, predicted))
        fp = sum(expected != label and actual == label for expected, actual in zip(gold, predicted))
        fn = sum(expected == label and actual != label for expected, actual in zip(gold, predicted))
        scores[label] = {
            "precision": tp / (tp + fp) if tp + fp else 0.0,
            "recall": tp / (tp + fn) if tp + fn else 0.0,
            "f1": _f1(tp, fp, fn),
            "tp": tp,
            "fp": fp,
            "fn": fn,
        }
    return scores


def _case_final_exact(case: dict[str, Any]) -> bool:
    gold = case["gold"]
    predicted = case["predicted_state"]
    expected_refs = set(gold.get("expected_active_memory_refs") or [])
    active_refs = set(predicted.get("expected_active_memory_refs") or [])
    all_by_ref = {row["memory_ref"]: row for row in predicted.get("all_memories", [])}
    terminal_refs = {row.get("memory_ref") for row in gold.get("final_memories", []) if row.get("memory_type") == "task" and _task_status(row.get("task_status")) == "done"}
    if expected_refs != active_refs | {ref for ref in terminal_refs if ref in all_by_ref}:
        return False
    if predicted.get("duplicate_active_count", 0) != gold.get("duplicate_active_count", 0):
        return False
    if predicted.get("stale_active_count", 0) != gold.get("stale_active_count", 0):
        return False
    by_ref = {row["memory_ref"]: row for row in predicted.get("active_memories", [])}
    for expected in gold.get("final_memories", []):
        actual = by_ref.get(expected["memory_ref"]) or all_by_ref.get(expected["memory_ref"])
        if actual is None:
            return False
        # `content` is intentionally excluded: several fixtures preserve legacy prose that contradicts their authoritative structured state.
        terminal = expected.get("memory_type") == "task" and _task_status(expected.get("task_status")) == "done"
        for field in ("memory_type", "entity", "attribute", "operation", "canonical_topic", "task_status", "old_value", "new_value", "status", "version_sequence", "source_note_ids", "valid_from", "valid_until", "polarity"):
            if terminal and field in {"status", "version_sequence", "source_note_ids", "valid_until"}:
                continue
            if field == "source_note_ids":
                if set(actual.get(field, [])) != set(expected.get(field, [])):
                    return False
            elif field == "task_status" and _task_status(actual.get(field)) != _task_status(expected.get(field)):
                return False
            elif field != "task_status" and actual.get(field) != expected.get(field):
                return False
    return True


def evaluate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    gold_relations: list[str] = []
    predicted_relations: list[str] = []
    gold_actions: list[str] = []
    predicted_actions: list[str] = []
    identity_tp = identity_fp = identity_fn = 0
    state_field_correct = Counter()
    state_field_total = Counter()
    final_state_field_correct = Counter()
    final_state_field_total = Counter()
    source_gold: list[str] = []
    source_predicted: list[str] = []
    pending_gold: list[bool] = []
    pending_predicted: list[bool] = []
    transition_cases = 0
    transition_exact = 0
    version_cases = 0
    version_exact = 0
    version_creation_cases = 0
    version_creation_exact = 0
    duplicate_version_creation = 0
    idempotence_cases = 0
    idempotence_exact = 0
    source_exact_cases = 0
    source_exact_correct = 0
    done_resolution_cases = 0
    done_resolution_correct = 0
    pending_side_effect_violations = 0
    orphan_cases = 0
    orphan_active_done = 0
    conversion_cases = 0
    conversion_correct = 0
    failures: list[dict[str, Any]] = []
    for case in cases:
        gold_decisions = case["gold"].get("decisions", [])
        predicted_decisions = case.get("predicted_decisions", [])
        raw_candidates = {str(row.get("candidate_id")): row for row in case["input"].get("incoming_candidates", [])}
        for index, expected in enumerate(gold_decisions):
            predicted = predicted_decisions[index] if index < len(predicted_decisions) else {}
            raw_candidate = raw_candidates.get(str(expected.get("candidate_id")), {})
            existing_updated = [str(row.get("updated_at") or "") for row in case["input"].get("existing_memories", [])]
            stale_candidate = bool(raw_candidate.get("observed_at") and existing_updated and str(raw_candidate.get("observed_at")) < max(existing_updated))
            gold_relations.append("conflict" if stale_candidate else str(expected.get("relation")))
            predicted_relations.append(str(predicted.get("relation")))
            gold_actions.append("pending_review" if stale_candidate else str(expected.get("action")))
            predicted_actions.append(str(predicted.get("action")))
            if raw_candidate.get("memory_type") == "task":
                expected_refs = set(expected.get("matched_memory_refs") or [])
                actual_refs = set(predicted.get("matched_memory_refs") or [])
                identity_tp += len(expected_refs & actual_refs)
                identity_fp += len(actual_refs - expected_refs)
                identity_fn += len(expected_refs - actual_refs)
            pending_gold.append(bool(expected.get("pending_review")) or stale_candidate)
            pending_predicted.append(bool(predicted.get("pending_review")))
            if expected.get("final_task_status") is not None:
                transition_cases += 1
                if predicted.get("target_memory_ref") == expected.get("target_memory_ref") and _task_status(predicted.get("final_task_status")) == _task_status(expected.get("final_task_status")) and predicted.get("action") == expected.get("action"):
                    transition_exact += 1
            if expected.get("final_task_status") is not None or expected.get("final_memory_type") is not None:
                for field in ("final_memory_type", "final_task_status", "expected_version_sequence"):
                    state_field_total[field] += 1
                    if field == "final_task_status" and _task_status(predicted.get(field)) == _task_status(expected.get(field)):
                        state_field_correct[field] += 1
                    elif field != "final_task_status" and predicted.get(field) == expected.get(field):
                        state_field_correct[field] += 1
            if expected.get("pending_review") and (predicted.get("create_version") or predicted.get("source_link_added")):
                pending_side_effect_violations += 1
            if expected.get("expected_version_sequence") is not None:
                version_cases += 1
                version_exact += int(predicted.get("expected_version_sequence") == expected.get("expected_version_sequence"))
            if expected.get("create_version") is not None:
                version_creation_cases += 1
                version_creation_exact += int(predicted.get("create_version") == expected.get("create_version"))
                if not expected.get("create_version") and predicted.get("create_version"):
                    duplicate_version_creation += 1
            tags = set(case.get("coverage_tags", []))
            if tags & {"duplicate_delivery", "same_duplicate_source", "concurrent_same", "concurrent_conflict"}:
                idempotence_cases += 1
                idempotence_exact += int(predicted.get("action") == expected.get("action") and predicted.get("create_version") == expected.get("create_version"))
        final_exact = _case_final_exact(case)
        case["case_exact"] = final_exact and len(gold_decisions) == len(predicted_decisions)
        if not case["case_exact"]:
            failures.append(case)
        if case.get("dataset") == "orphan_done_resolution" and any(row.get("memory_type") == "task" and _task_status(row.get("task_status")) == "done" for row in case["input"].get("incoming_candidates", [])):
            done_resolution_cases += 1
            done_resolution_correct += int(
                all(
                    (_task_status(predicted.get(key)) == _task_status(expected.get(key)) if key == "final_task_status" else predicted.get(key) == expected.get(key))
                    for expected, predicted in zip(case["gold"].get("decisions", []), case.get("predicted_decisions", []))
                    for key in ("matched_memory_refs", "relation", "action", "target_memory_ref", "final_memory_type", "final_task_status", "pending_review")
                )
            )
            if not case["input"].get("existing_memories"):
                orphan_cases += 1
                active_done = any(row.get("memory_type") == "task" and _task_status(row.get("task_status")) == "done" for row in case["predicted_state"].get("active_memories", []))
                orphan_active_done += int(active_done)
            if "convert_to_episodic" in case.get("coverage_tags", []):
                conversion_cases += 1
                expected_type = next((row.get("memory_type") for row in case["gold"].get("final_memories", [])), "episodic")
                predicted_type = next((row.get("memory_type") for row in case["predicted_state"].get("active_memories", [])), None)
                conversion_correct += int(predicted_type == expected_type)

        # Source-link quality is measured against final persisted source-note pairs,
        # not against the identity target itself.
        for expected_memory in case["gold"].get("final_memories", []):
            ref = expected_memory.get("memory_ref")
            for note_id in expected_memory.get("source_note_ids", []):
                source_gold.append(f"{ref}|{note_id}")
        predicted_by_ref = {row.get("memory_ref"): row for row in case["predicted_state"].get("active_memories", [])}
        for expected_memory in case["gold"].get("final_memories", []):
            ref = expected_memory.get("memory_ref")
            actual_memory = predicted_by_ref.get(ref)
            for note_id in (actual_memory or {}).get("source_note_ids", []):
                source_predicted.append(f"{ref}|{note_id}")
        expected_source_sets = {row.get("memory_ref"): set(row.get("source_note_ids", [])) for row in case["gold"].get("final_memories", [])}
        predicted_source_sets = {row.get("memory_ref"): set(row.get("source_note_ids", [])) for row in case["predicted_state"].get("active_memories", [])}
        if expected_source_sets:
            source_exact_cases += 1
            source_exact_correct += int(all(predicted_source_sets.get(ref, set()) == notes for ref, notes in expected_source_sets.items()))

        state_fields = ("memory_type", "status", "task_status", "entity", "attribute", "operation", "canonical_topic", "old_value", "new_value", "polarity", "valid_from", "valid_until")
        for expected_memory in case["gold"].get("final_memories", []):
            actual_memory = predicted_by_ref.get(expected_memory.get("memory_ref"), {})
            for field in state_fields:
                final_state_field_total[field] += 1
                if field == "task_status":
                    final_state_field_correct[field] += int(_task_status(actual_memory.get(field)) == _task_status(expected_memory.get(field)))
                else:
                    final_state_field_correct[field] += int(actual_memory.get(field) == expected_memory.get(field))

    relation_scores = _per_label_scores(gold_relations, predicted_relations, EVAL_RELATIONS)
    action_scores = _per_label_scores(gold_actions, predicted_actions, EVAL_ACTIONS)
    relation_macro_f1 = sum(item["f1"] for item in relation_scores.values()) / len(EVAL_RELATIONS)
    pending = _binary_prf(pending_gold, pending_predicted)
    identity = {"precision": identity_tp / (identity_tp + identity_fp) if identity_tp + identity_fp else 0.0, "recall": identity_tp / (identity_tp + identity_fn) if identity_tp + identity_fn else 0.0, "f1": _f1(identity_tp, identity_fp, identity_fn), "tp": identity_tp, "fp": identity_fp, "fn": identity_fn}
    return {
        "cases": len(cases),
        "decision_count": len(gold_relations),
        "case_exact_match": sum(case.get("case_exact", False) for case in cases) / len(cases) if cases else 0.0,
        "task_identity": identity,
        "no_match_accuracy": sum(not expected.get("matched_memory_refs") and not predicted.get("matched_memory_refs") for case in cases for expected, predicted in zip(case["gold"].get("decisions", []), case.get("predicted_decisions", []))) / max(1, sum(not expected.get("matched_memory_refs") for case in cases for expected in case["gold"].get("decisions", []))),
        "relation_macro_f1": relation_macro_f1,
        "relation_scores": relation_scores,
        "relation_confusion": _confusion(gold_relations, predicted_relations, EVAL_RELATIONS),
        "action_accuracy": sum(expected == predicted for expected, predicted in zip(gold_actions, predicted_actions)) / len(gold_actions) if gold_actions else 0.0,
        "action_scores": action_scores,
        "action_confusion": _confusion(gold_actions, predicted_actions, EVAL_ACTIONS),
        "current_state_field_accuracy": {field: state_field_correct[field] / state_field_total[field] if state_field_total[field] else 0.0 for field in state_field_total},
        "final_state_field_accuracy": {field: final_state_field_correct[field] / final_state_field_total[field] if final_state_field_total[field] else 0.0 for field in final_state_field_total},
        "task_transition_accuracy": transition_exact / transition_cases if transition_cases else 0.0,
        "version_sequence_accuracy": version_exact / version_cases if version_cases else 0.0,
        "version_sequence_cases": version_cases,
        "version_creation_accuracy": version_creation_exact / version_creation_cases if version_creation_cases else 0.0,
        "duplicate_version_creation_count": duplicate_version_creation,
        "idempotence_accuracy": idempotence_exact / idempotence_cases if idempotence_cases else 0.0,
        "idempotence_cases": idempotence_cases,
        "pending_review": pending,
        "duplicate_active_rate": sum(case["predicted_state"].get("duplicate_active_count", 0) > 0 for case in cases) / len(cases) if cases else 0.0,
        "stale_active_rate": sum(case["predicted_state"].get("stale_active_count", 0) > 0 for case in cases) / len(cases) if cases else 0.0,
        "orphan_done_task_rate": orphan_active_done / orphan_cases if orphan_cases else 0.0,
        "orphan_done_cases": orphan_cases,
        "conversion_to_episodic_accuracy": conversion_correct / conversion_cases if conversion_cases else 0.0,
        "source_link": _prf(source_gold, source_predicted),
        "source_exact_set_accuracy": source_exact_correct / source_exact_cases if source_exact_cases else 0.0,
        "done_resolution_accuracy": done_resolution_correct / done_resolution_cases if done_resolution_cases else 0.0,
        "done_resolution_cases": done_resolution_cases,
        "pending_side_effect_violations": pending_side_effect_violations,
        "failure_count": len(failures),
        "failures": failures,
    }
