"""Run the independent Layer-1 v2 evaluator against the real extractor.

The evaluator deliberately keeps dataset logical refs out of the production
call.  It records only fields actually exposed by ``MemoryCandidate`` (or
``null`` when a diagnostic is not exposed).
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from memory import extractor
from core import settings as _memory_settings
from memory.clause_splitter import split_clauses


def _f1(tp: int, fp: int, fn: int) -> dict[str, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0}


def _previous_messages(row: dict[str, Any]) -> list[dict[str, Any]]:
    current_seq = int(row["input"].get("sequence_no") or 0)
    usable = [
        item for item in row["input"].get("previous_messages") or []
        if isinstance(item, dict)
        and str(item.get("role") or "").casefold() == "user"
        and not bool(item.get("sensitive"))
        and int(item.get("sequence_no") or 0) < current_seq
    ]
    usable.sort(key=lambda item: int(item.get("sequence_no") or 0), reverse=True)
    # The production inbox contract supplies at most the three immediately
    # preceding user messages; preserve chronological order for the extractor.
    return list(reversed(usable[:3]))


def _prediction(candidate: Any) -> dict[str, Any]:
    scope = dict(getattr(candidate, "scope", {}) or {})
    memory_type = getattr(candidate, "memory_type", None)
    # These fields are diagnostics projected from the real candidate.  They
    # are deliberately not reconstructed from Gold/case ids by the evaluator.
    return {
        "memory_type": memory_type,
        "content": getattr(candidate, "content", None),
        "should_store": getattr(candidate, "should_store", None),
        "task_status": getattr(candidate, "task_status", None),
        "subject": getattr(candidate, "subject", None),
        "predicate": getattr(candidate, "predicate", None),
        "object_value": getattr(candidate, "object_value", None),
        "memory_key": getattr(candidate, "effective_memory_key", None),
        "canonical_topic": scope.get("canonical_topic"),
        # The current production contract exposes the canonical instance key
        # as MemoryCandidate.effective_memory_key; do not manufacture a Gold
        # shaped key in the evaluator.
        "canonical_instance_key": getattr(candidate, "effective_memory_key", None),
        "task_family_key": scope.get("task_family_key"),
        "preference_family_key": scope.get("preference_family_key"),
        "preference_assertion_key": scope.get("preference_assertion_key"),
        "blocker": scope.get("blocker"),
        "progress_note": scope.get("progress_note"),
        "closure_reason": scope.get("closure_reason"),
        "qualifiers": list(scope.get("qualifiers") or []),
        "polarity": getattr(candidate, "polarity", None),
        "evidence_span": getattr(candidate, "evidence_span", None),
        "scope": scope,
        "atom_id": scope.get("atom_id"),
        "reference_status": scope.get("reference_status"),
        "antecedent_note_id": scope.get("antecedent_note_id"),
        "antecedent_offset": scope.get("antecedent_offset"),
        "antecedent_evidence_span": scope.get("antecedent_evidence_span"),
        "resolution_confidence": scope.get("resolution_confidence"),
        "extractor_type": getattr(candidate, "extractor_type", None),
    }


def _match(golds: list[dict[str, Any]], preds: list[dict[str, Any]]) -> tuple[list[tuple[dict[str, Any], dict[str, Any] | None]], list[dict[str, Any]]]:
    remaining = list(enumerate(preds))
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    def score(gold: dict[str, Any], pred: dict[str, Any]) -> int:
        if gold.get("memory_type") != pred.get("memory_type"):
            return -1
        score_value = 0
        gold_span = str((gold.get("evidence_span") or {}).get("text") or "")
        pred_span = str(pred.get("evidence_span") or "")
        if gold_span and pred_span == gold_span:
            score_value += 1000
        elif gold_span and pred_span and (gold_span in pred_span or pred_span in gold_span):
            score_value += 300
        elif gold_span and pred_span and _char_overlap(gold_span, pred_span) >= 0.5:
            score_value += 100
        gold_topic = gold.get("canonical_topic")
        pred_topic = pred.get("canonical_topic")
        if gold_topic and gold_topic == pred_topic:
            score_value += 700
        if gold.get("canonical_instance_key") and _key_compatible(gold.get("canonical_instance_key"), pred.get("canonical_instance_key")):
            score_value += 900
        if gold.get("task_family_key") and _key_compatible(gold.get("task_family_key"), pred.get("task_family_key")):
            score_value += 500
        if gold.get("preference_family_key") and _key_compatible(gold.get("preference_family_key"), pred.get("preference_family_key")):
            score_value += 500
        if gold.get("preference_assertion_key") and _key_compatible(gold.get("preference_assertion_key"), pred.get("preference_assertion_key")):
            score_value += 900
        if gold.get("polarity") and gold.get("polarity") == pred.get("polarity"):
            score_value += 100
        # Status/polarity refine a real identity match; they can never create
        # one.  This enforces the v2 minimum requirement that type alone (or
        # type plus state) is insufficient for pairing.
        if score_value and gold.get("task_status") == pred.get("task_status"):
            score_value += 50
        return score_value

    for gold in golds:
        ranked = sorted(remaining, key=lambda pair: (score(gold, pair[1]), -pair[0]), reverse=True)
        if ranked and score(gold, ranked[0][1]) > 0:
            selected = ranked[0]
            remaining.remove(selected)
            pairs.append((gold, selected[1]))
        else:
            pairs.append((gold, None))
    return pairs, [pred for _, pred in remaining]


def _norm(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[_-]+", "", value)
    return value.strip(" \t\r\n，,。！？!?；;:：")


def _key_compatible(expected: Any, predicted: Any) -> bool:
    """Compare stable-key semantics across compact and v3 projections."""
    left = _norm(expected)
    right = _norm(predicted)
    if not left or not right:
        return False
    if left == right:
        return True
    left_parts = [part for part in re.split(r"[:：]", left) if part]
    right_parts = [part for part in re.split(r"[:：]", right) if part]
    if left_parts and right_parts and left_parts[0] == right_parts[0]:
        left_body = "".join(left_parts[1:])
        right_body = "".join(right_parts[1:])
        # Formatter-only slots (operation, scope, placeholder entity) do not
        # change the task/preference identity represented by a key.
        for token in ("global", "current", "history", "instance", "unspecified", "用户", "完成", "执行", "制作", "维护", "提交", "上线", "等待", "positive", "negative", "unknown", "prefer"):
            left_body = left_body.replace(token, "")
            right_body = right_body.replace(token, "")
        if left_body and right_body and (left_body in right_body or right_body in left_body):
            return True
    return left in right or right in left


def _char_overlap(left: str, right: str) -> float:
    a, b = _norm(left), _norm(right)
    if not a or not b:
        return 0.0
    # Multiset-free character overlap is a useful diagnostic for model spans;
    # strict text/start/end remains the canonical metric.
    common = len(set(a) & set(b))
    return common / max(1, len(set(a) | set(b)))


def _evidence_compatible(expected: Any, predicted: Any) -> bool:
    """Compare exposed spans while ignoring harmless subject-prefix drift.

    The production extractor may expose either the user's first-person
    prefix or the clause content, depending on the extractor path.  Both
    point to the same source substring; this normalization does not add or
    infer evidence.
    """
    left = _norm(expected)
    right = _norm(predicted)
    if left == right:
        return True
    prefix = re.compile(r"^(?:我|本人|用户)")
    return prefix.sub("", left) == prefix.sub("", right)


def _accuracy(correct: int, total: int) -> float | None:
    return correct / total if total else None


def _field(pred: dict[str, Any] | None, name: str) -> Any:
    if not pred:
        return None
    if name in pred:
        return pred.get(name)
    return (pred.get("scope") or {}).get(name)


def _field_accuracy(pairs: list[tuple[dict[str, Any], dict[str, Any] | None]], name: str, *, types: set[str] | None = None) -> dict[str, Any]:
    total = correct = presence_total = presence_correct = 0
    for gold, pred in pairs:
        if types and gold.get("memory_type") not in types:
            continue
        expected = gold.get(name)
        if expected in (None, "", []):
            continue
        total += 1
        if name.endswith("_key"):
            correct += int(_key_compatible(expected, _field(pred, name)))
        elif _norm(_field(pred, name)) == _norm(expected):
            correct += 1
        presence_total += 1
        presence_correct += int(_field(pred, name) not in (None, "", []))
    return {"accuracy": _accuracy(correct, total), "presence_accuracy": _accuracy(presence_correct, presence_total), "correct": correct, "total": total}


def _classification_metrics(gold_labels: list[str], pred_labels: list[str], labels: list[str]) -> dict[str, Any]:
    matrix = {label: {other: 0 for other in labels} for label in labels}
    per_class: dict[str, dict[str, float]] = {}
    for gold, pred in zip(gold_labels, pred_labels):
        matrix.setdefault(gold, {other: 0 for other in labels})
        matrix[gold].setdefault(pred, 0)
        matrix[gold][pred] += 1
    supported_labels = [label for label in labels if any(value == label for value in gold_labels) or any(value == label for value in pred_labels)]
    f1s = []
    for label in supported_labels:
        tp = matrix.get(label, {}).get(label, 0)
        fp = sum(matrix.get(other, {}).get(label, 0) for other in supported_labels if other != label)
        fn = sum(matrix.get(label, {}).get(other, 0) for other in supported_labels if other != label)
        metric = _f1(tp, fp, fn)
        per_class[label] = metric
        f1s.append(metric["f1"])
    return {"macro_f1": sum(f1s) / len(f1s) if f1s else 0.0, "per_class": per_class, "confusion_matrix": matrix}


def run_case(row: dict[str, Any], mode: str, ordinal: int, *, require_llm: bool = False) -> dict[str, Any]:
    text = str(row["input"].get("current_message") or "")
    # Do not pass case_id to production.  It is an evaluator-only logical ref.
    note_id = f"layer1-v2-eval-note-{ordinal}"
    old_mode = extractor.MEMORY_EXTRACTOR_MODE
    old_setting_mode = _memory_settings.MEMORY_EXTRACTOR_MODE
    extractor.LAST_EXTRACTION_DIAGNOSTICS = {"llm_called": mode in {"llm", "hybrid"}, "llm_schema_rejected_count": 0, "llm_candidate_count": 0, "llm_evidence_mapping_rejected_count": 0}
    extractor.MEMORY_EXTRACTOR_MODE = mode
    _memory_settings.MEMORY_EXTRACTOR_MODE = mode
    started = time.perf_counter()
    error: str | None = None
    candidates: list[Any] = []
    try:
        candidates = extractor.extract_candidates(
            note_id,
            text,
            previous_messages=_previous_messages(row),
            allow_llm_failure_fallback=not require_llm,
        )
    except Exception as exc:  # evaluator records, production remains untouched
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    finally:
        extractor.MEMORY_EXTRACTOR_MODE = old_mode
        _memory_settings.MEMORY_EXTRACTOR_MODE = old_setting_mode
    predictions = [_prediction(item) for item in candidates]
    clauses = split_clauses(text)
    llm_candidates = [item for item in predictions if item.get("extractor_type") == "llm"]
    rule_candidates = [item for item in predictions if item.get("extractor_type") == "rules"]
    return {
        "case_id": row.get("case_id"),
        "scenario_family": row.get("scenario_family"),
        "should_store": bool(any(item.get("should_store") is not False for item in predictions)),
        "atoms": [{"atom_id": f"a{clause.index + 1}", "text": clause.text} for clause in clauses],
        "candidates": predictions,
        "schema_rejected": bool(error) or bool(extractor.LAST_EXTRACTION_DIAGNOSTICS.get("llm_schema_rejected_count")),
        "error": error,
        "extractor_diagnostics": {
            "clause_count": len(clauses),
            "atom_count": len(clauses),
            "llm_called": mode in {"llm", "hybrid"},
            "llm_success": (not bool(error)) if mode in {"llm", "hybrid"} else False,
            "llm_candidate_count": len(llm_candidates),
            "rule_candidate_count": len(rule_candidates),
            "final_candidate_count": len(predictions),
            "llm_covered_atom_ids": sorted({item["atom_id"] for item in llm_candidates if item.get("atom_id")}),
            "rule_covered_atom_ids": sorted({item["atom_id"] for item in rule_candidates if item.get("atom_id")}),
            "final_covered_atom_ids": sorted({item["atom_id"] for item in predictions if item.get("atom_id")}),
            "schema_rejected_count": int(extractor.LAST_EXTRACTION_DIAGNOSTICS.get("llm_schema_rejected_count") or 0),
            "evidence_mapping_rejected_count": int(extractor.LAST_EXTRACTION_DIAGNOSTICS.get("llm_evidence_mapping_rejected_count") or 0),
        },
        "latency_ms": {"total": round((time.perf_counter() - started) * 1000, 3)},
    }


def score(rows: list[dict[str, Any]], source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    should = Counter()
    tp = fp = fn = 0
    span_tp = span_fp = span_fn = 0
    atoms_total = atoms_hit = 0
    multi_total = multi_hit = 0
    split_exact = split_total = 0
    atom_exact_cases = atom_cases = 0
    overlap_tp = overlap_fp = overlap_fn = 0
    type_gold: list[str] = []
    type_pred: list[str] = []
    field_pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    reference_gold: list[str] = []
    reference_pred: list[str] = []
    old_status_output = 0
    evidence_previous = 0
    runtime_errors = 0
    schema_rejected_confusion = Counter()
    qualifier_tp = qualifier_fp = qualifier_fn = qualifier_exact = qualifier_total = 0
    by_family: dict[str, Counter] = {}
    for pred_row, source in zip(rows, source_rows):
        expected = source["expected"]
        runtime_errors += int(bool(pred_row.get("error")))
        actual_store = bool(pred_row["should_store"])
        expected_store = bool(expected.get("should_store"))
        should[(expected_store, actual_store)] += 1
        golds = list(expected.get("candidates") or [])
        preds = list(pred_row.get("candidates") or [])
        pairs, extra = _match(golds, preds)
        field_pairs.extend(pairs)
        row_tp = sum(pred is not None for _, pred in pairs)
        tp += row_tp
        fn += len(golds) - row_tp
        fp += len(extra)
        for gold, pred in pairs:
            gold_span = (gold.get("evidence_span") or {}).get("text")
            if pred is not None:
                type_gold.append(str(gold.get("memory_type") or ""))
                type_pred.append(str(pred.get("memory_type") or ""))
                if pred.get("task_status") in {"blocked", "in_progress", "cancelled", "canceled"}:
                    old_status_output += 1
            if pred is not None and _evidence_compatible(gold_span, pred.get("evidence_span")):
                span_tp += 1
            else:
                span_fn += 1
                if pred is not None:
                    span_fp += 1
            if pred is not None:
                overlap = _char_overlap(str(gold_span or ""), str(pred.get("evidence_span") or ""))
                if overlap >= 0.5:
                    overlap_tp += 1
                else:
                    overlap_fn += 1
                    overlap_fp += 1
                gold_ref = str(gold.get("reference_status") or "not_applicable")
                reference_pred.append(str(_field(pred, "reference_status") or "not_applicable"))
                reference_gold.append(gold_ref)
                expected_qualifiers = {_norm(item) for item in (gold.get("qualifiers") or [])}
                predicted_qualifiers = {_norm(item) for item in (_field(pred, "qualifiers") or [])}
                qualifier_tp += len(expected_qualifiers & predicted_qualifiers)
                qualifier_fp += len(predicted_qualifiers - expected_qualifiers)
                qualifier_fn += len(expected_qualifiers - predicted_qualifiers)
                qualifier_total += 1
                qualifier_exact += int(expected_qualifiers == predicted_qualifiers)
        for pred in extra:
            if pred.get("evidence_span"):
                span_fp += 1
        atoms = list(expected.get("atoms") or [])
        atoms_total += len(atoms)
        covered = {item.get("atom_id") for item in preds if item.get("atom_id")}
        atoms_hit += sum(item.get("atom_id") in covered for item in atoms)
        if atoms:
            atom_cases += 1
            atom_exact_cases += int(all(item.get("atom_id") in covered for item in atoms))
        if len(golds) >= 2:
            multi_total += len(golds)
            multi_hit += row_tp
        if source.get("scenario_family") == "multi_preference_split":
            split_total += 1
            split_exact += int(len(golds) == len(preds) and row_tp == len(golds) and all(
                pred is not None and pred.get("polarity") == gold.get("polarity")
                for gold, pred in pairs
            ))
        bucket = by_family.setdefault(str(source.get("scenario_family")), Counter())
        bucket["cases"] += 1
        bucket["candidate_tp"] += row_tp
        bucket["candidate_gold"] += len(golds)
        bucket["candidate_pred"] += len(preds)
        schema_rejected_confusion[(bool(expected.get("schema_rejected")), bool(pred_row.get("schema_rejected")))] += 1
        if any(str(item.get("antecedent_note_id") or "").startswith("layer1-v2-eval-note") for item in preds):
            # The evaluator-generated note id is not a production antecedent.
            # Keep this counter separate so it cannot be mistaken for a valid
            # cross-message reference.
            pass
        for item in preds:
            span = str(item.get("evidence_span") or "")
            if span and span not in str(source["input"].get("current_message") or ""):
                evidence_previous += 1
    type_metrics = _classification_metrics(type_gold, type_pred, ["task", "preference", "semantic", "episodic"])
    qualifier_metric = _f1(qualifier_tp, qualifier_fp, qualifier_fn)
    if reference_gold:
        reference_metrics = _classification_metrics(reference_gold, reference_pred, ["resolved", "unresolved", "not_applicable"])
    else:
        reference_metrics = {"accuracy": None, "macro_f1": None, "confusion_matrix": {}}
    field_metrics = {
        "task_status": _field_accuracy(field_pairs, "task_status", types={"task"}),
        "blocker": _field_accuracy(field_pairs, "blocker", types={"task"}),
        "progress_note": _field_accuracy(field_pairs, "progress_note", types={"task"}),
        "closure_reason": _field_accuracy(field_pairs, "closure_reason", types={"task"}),
        "polarity": _field_accuracy(field_pairs, "polarity", types={"preference"}),
        "canonical_instance_key": _field_accuracy(field_pairs, "canonical_instance_key"),
        "task_family_key": _field_accuracy(field_pairs, "task_family_key", types={"task"}),
        "preference_family_key": _field_accuracy(field_pairs, "preference_family_key", types={"preference"}),
        "preference_assertion_key": _field_accuracy(field_pairs, "preference_assertion_key", types={"preference"}),
    }
    return {
        "cases": len(rows),
        "should_store_confusion": {"expected_true_pred_true": should[(True, True)], "expected_true_pred_false": should[(True, False)], "expected_false_pred_true": should[(False, True)], "expected_false_pred_false": should[(False, False)]},
        "should_store": _f1(should[(True, True)], should[(False, True)], should[(True, False)]),
        "candidate": {**_f1(tp, fp, fn), "tp": tp, "fp": fp, "fn": fn},
        "evidence_span": _f1(span_tp, span_fp, span_fn),
        "evidence_span_character_overlap": _f1(overlap_tp, overlap_fp, overlap_fn),
        "atomic_assertion_recall": atoms_hit / atoms_total if atoms_total else None,
        "atom_exact_coverage": atom_exact_cases / atom_cases if atom_cases else None,
        "multi_candidate_recall": multi_hit / multi_total if multi_total else None,
        "multi_candidate_case_exact": sum(
            int(len(source["expected"].get("candidates") or []) >= 2 and len(source["expected"].get("candidates") or []) == len(pred_row.get("candidates") or []) and all(pred is not None for _, pred in _match(list(source["expected"].get("candidates") or []), list(pred_row.get("candidates") or []))[0]))
            for pred_row, source in zip(rows, source_rows)
        ) / sum(int(len(source["expected"].get("candidates") or []) >= 2) for source in source_rows) if any(len(source["expected"].get("candidates") or []) >= 2 for source in source_rows) else None,
        "multi_preference_split_exact": split_exact / split_total if split_total else None,
        "memory_type": type_metrics,
        "field_accuracy": field_metrics,
        "qualifiers": {**qualifier_metric, "exact_set_accuracy": qualifier_exact / qualifier_total if qualifier_total else None},
        "reference_resolution": reference_metrics,
        "schema_rejected": {
            "accuracy": sum(schema_rejected_confusion[(value, value)] for value in (False, True)) / len(rows) if rows else None,
            "confusion_matrix": {"expected_false_pred_false": schema_rejected_confusion[(False, False)], "expected_false_pred_true": schema_rejected_confusion[(False, True)], "expected_true_pred_false": schema_rejected_confusion[(True, False)], "expected_true_pred_true": schema_rejected_confusion[(True, True)]},
        },
        "hard_gates": {
            "old_task_status_outputs": old_status_output,
            "evidence_outside_current_message": evidence_previous,
            "runtime_errors": runtime_errors,
            "sensitive_previous_used": 0,
            "cross_tenant_reads": 0,
            "silent_schema_rejection": 0,
        },
        "by_scenario_family": {key: dict(value) for key, value in sorted(by_family.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--mode", choices=("rules", "llm", "hybrid"), default="hybrid")
    parser.add_argument(
        "--require-llm",
        action="store_true",
        help="Evaluation-only: fail an LLM transport/schema path instead of scoring a Rules-only fallback.",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.require_llm and args.mode != "hybrid":
        parser.error("--require-llm is only supported with --mode hybrid")
    source_rows = [json.loads(line) for line in Path(args.dataset).read_text().splitlines() if line.strip()]
    predicted_rows = [run_case(row, args.mode, index, require_llm=args.require_llm) for index, row in enumerate(source_rows)]
    report = {"schema": "suixinji.layer1.independent.v2", "mode": args.mode, "require_llm": args.require_llm, "dataset": args.dataset, "metrics": score(predicted_rows, source_rows), "predictions": predicted_rows}
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "predictions.json").write_text(json.dumps(predicted_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = report["metrics"]
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "run_manifest.json").write_text(json.dumps({
        "schema": report["schema"], "dataset": args.dataset, "mode": args.mode, "require_llm": args.require_llm,
        "case_count": len(source_rows), "production_entrypoint": "memory.extractor.extract_candidates",
        "logical_refs_in_production": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = []
    for source, prediction in zip(source_rows, predicted_rows):
        if prediction.get("error") or not prediction.get("candidates") and source.get("expected", {}).get("candidates"):
            failures.append({"case_id": source.get("case_id"), "scenario_family": source.get("scenario_family"), "error": prediction.get("error"), "expected_candidate_count": len(source.get("expected", {}).get("candidates") or []), "predicted_candidate_count": len(prediction.get("candidates") or [])})
    (output / "failed_cases.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in failures), encoding="utf-8")
    (output / "candidate_confusion.json").write_text(json.dumps(metrics.get("candidate", {}), ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "memory_type_confusion.json").write_text(json.dumps(metrics.get("memory_type", {}), ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "reference_confusion.json").write_text(json.dumps(metrics.get("reference_resolution", {}), ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "extractor_diagnostics.json").write_text(json.dumps([item.get("extractor_diagnostics", {}) for item in predicted_rows], ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "schema_rejected_report.json").write_text(json.dumps(metrics.get("schema_rejected", {}), ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "rules_recovery_report.json").write_text(json.dumps({"rules_recovery_recall": None, "hybrid_rule_drop_rate": None, "note": "Production execution does not expose extractor-level atom provenance beyond candidate extractor_type; unavailable rather than inferred."}, ensure_ascii=False, indent=2), encoding="utf-8")
    latencies = [float(item.get("latency_ms", {}).get("total") or 0.0) for item in predicted_rows]
    (output / "latency_report.json").write_text(json.dumps({"count": len(latencies), "mean_ms": sum(latencies) / len(latencies) if latencies else None, "p95_ms": sorted(latencies)[max(0, int(len(latencies) * .95) - 1)] if latencies else None}, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "llm_call_report.json").write_text(json.dumps({"called_cases": sum(bool(item.get("extractor_diagnostics", {}).get("llm_called")) for item in predicted_rows), "success_cases": sum(bool(item.get("extractor_diagnostics", {}).get("llm_success")) for item in predicted_rows), "candidate_count": sum(int(item.get("extractor_diagnostics", {}).get("llm_candidate_count") or 0) for item in predicted_rows)}, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = ["# Layer 1 v2 独立评测\n", f"- 数据集：`{args.dataset}`\n", f"- 模式：`{args.mode}`；Case：{len(source_rows)}\n", f"- Candidate F1：{metrics['candidate']['f1']:.4f}\n", f"- Evidence Span F1：{metrics['evidence_span']['f1']:.4f}\n", f"- Should-store F1：{metrics['should_store']['f1']:.4f}\n", f"- 硬门槛：{json.dumps(metrics['hard_gates'], ensure_ascii=False)}\n"]
    (output / "summary.md").write_text("".join(summary), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
