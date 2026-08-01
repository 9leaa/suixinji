"""Offline Layer-1 regression runner using the repository metric contract."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory import extractor
from memory.canonicalizer import preference_key, semantic_key, task_key
from memory.models import memory_key_for
from memory.policies.preference import preference_polarity

FIELDS = [
    "entity",
    "attribute",
    "operation",
    "canonical_topic",
    "task_status",
    "old_value",
    "new_value",
    "valid_from",
    "valid_until",
    "polarity",
    "memory_key",
]
KEY_FIELDS = FIELDS[:7]
TYPES = ["preference", "task", "semantic", "episodic"]


def clean(value: Any) -> Any:
    if value == "":
        return None
    return value


def equal(left: Any, right: Any) -> bool:
    left, right = clean(left), clean(right)
    if left is None or right is None:
        return left is right
    return str(left).strip() == str(right).strip()


def expected_key(row: dict[str, Any], candidate: dict[str, Any]) -> str:
    typ = candidate["memory_type"]
    entity = candidate.get("entity") or "用户"
    attribute = candidate.get("attribute") or typ
    topic = candidate.get("canonical_topic") or candidate.get("new_value") or ""
    operation = candidate.get("operation") or "维护"
    if typ == "task":
        return task_key(entity, attribute, operation, "global")
    if typ == "semantic":
        return semantic_key(entity, attribute, topic, "current")
    if typ == "preference":
        return preference_key(entity, topic, "global")
    return memory_key_for(
        typ,
        subject=entity,
        predicate=attribute,
        object_value=candidate.get("new_value") or topic,
        content=candidate.get("content") or row["input"]["text"],
    )


def gold_candidates(row: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for original in row.get("expected_output", {}).get("candidates", []):
        candidate = dict(original)
        candidate["polarity"] = preference_polarity(row["input"]["text"]) if candidate.get("memory_type") == "preference" else None
        candidate["memory_key"] = expected_key(row, candidate)
        result.append(
            {field: candidate.get(field) for field in ["memory_type", *FIELDS]} | {"evidence_span": candidate.get("evidence_span")}
        )
    return result


def predicted(candidate: Any) -> dict[str, Any]:
    scope = dict(getattr(candidate, "scope", {}) or {})
    typ = getattr(candidate, "memory_type", None)
    value = getattr(candidate, "object_value", None)
    return {
        "memory_type": typ,
        "entity": getattr(candidate, "subject", None),
        "attribute": getattr(candidate, "predicate", None),
        "operation": scope.get("operation"),
        "canonical_topic": scope.get("canonical_topic") or value,
        "task_status": getattr(candidate, "task_status", None),
        "old_value": scope.get("old_value"),
        "new_value": scope.get("new_value") or (value if typ in {"preference", "semantic", "episodic"} else None),
        "valid_from": getattr(candidate, "valid_from", None),
        "valid_until": getattr(candidate, "valid_until", None),
        "polarity": getattr(candidate, "polarity", None),
        "memory_key": getattr(candidate, "effective_memory_key", None),
        "evidence_span": getattr(candidate, "evidence_span", None),
    }


def match(
    golds: list[dict[str, Any]], preds: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any] | None]], list[dict[str, Any]]]:
    remaining = list(preds)
    pairs = []
    for gold in golds:

        def score(pred: dict[str, Any]) -> int:
            if pred.get("memory_type") != gold.get("memory_type"):
                return -1
            if pred.get("evidence_span") and pred.get("evidence_span") == gold.get("evidence_span"):
                return 400
            if pred.get("canonical_topic") and pred.get("canonical_topic") == gold.get("canonical_topic"):
                return 300
            if pred.get("memory_key") and pred.get("memory_key") == gold.get("memory_key"):
                return 250
            return 100

        ranked = sorted(enumerate(remaining), key=lambda item: (score(item[1]), -item[0]), reverse=True)
        if ranked and score(ranked[0][1]) >= 100:
            index, pred = ranked[0]
            remaining.pop(index)
        else:
            pred = None
        pairs.append((gold, pred))
    return pairs, remaining


def f1(tp: int, fp: int, fn: int) -> float:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def candidate_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = fn = 0
    gold_types, pred_types = Counter(), Counter()
    for row in rows:
        pairs, extra = match(row["gold"], row["pred"])
        tp += sum(pred is not None for _, pred in pairs)
        fn += sum(pred is None for _, pred in pairs)
        fp += len(extra)
        gold_types.update(item["memory_type"] for item in row["gold"])
        pred_types.update(item["memory_type"] for item in row["pred"])
    by_type = {}
    for typ in TYPES:
        true_positive = sum(
            1 for row in rows for gold, pred in match(row["gold"], row["pred"])[0] if gold["memory_type"] == typ and pred is not None
        )
        false_negative = gold_types[typ] - true_positive
        false_positive = max(0, pred_types[typ] - true_positive)
        by_type[typ] = f1(true_positive, false_positive, false_negative)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "candidate_f1": f1(tp, fp, fn),
        "memory_type_macro_f1": sum(by_type.values()) / len(TYPES),
        "memory_type_f1": by_type,
    }


def field_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    correct, total = Counter(), Counter()
    exact = 0
    for row in rows:
        for gold, pred in match(row["gold"], row["pred"])[0]:
            all_correct = pred is not None
            for field in FIELDS:
                total[field] += 1
                good = pred is not None and equal(gold.get(field), pred.get(field))
                if good:
                    correct[field] += 1
                else:
                    all_correct = False
            exact += int(all_correct)
    accuracy = {field: correct[field] / total[field] if total[field] else 0.0 for field in FIELDS}
    gold_count = total[FIELDS[0]] if FIELDS else 0
    return {
        "correct": dict(correct),
        "total": dict(total),
        "accuracy": accuracy,
        "key_field_accuracy": sum(correct[field] for field in KEY_FIELDS) / (gold_count * len(KEY_FIELDS)) if gold_count else 0.0,
        "all_fields_exact": exact / gold_count if gold_count else 0.0,
        "all_fields_exact_count": exact,
        "gold_candidates": gold_count,
    }


def should_store_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = fn = 0
    for row in rows:
        gold_store = any(item.get("should_store", True) for item in row["raw_gold"])
        pred_store = bool(row["pred"])
        if gold_store and pred_store:
            tp += 1
        elif not gold_store and pred_store:
            fp += 1
        elif gold_store and not pred_store:
            fn += 1
    return {"tp": tp, "fp": fp, "fn": fn, "should_store_f1": f1(tp, fp, fn)}


def load_rows(zip_path: Path, dataset: str) -> list[dict[str, Any]]:
    with zipfile.ZipFile(zip_path) as archive:
        return [json.loads(line) for line in archive.read(dataset).decode().splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/home/zcj/suixinji")
    parser.add_argument("--mode", choices=["rules", "llm"], default="rules")
    parser.add_argument("--dataset", default="all")
    args = parser.parse_args()
    root = Path(args.root)
    archive = root / "suixinji_layer1_repaired_datasets.zip"
    available = ["should_store_basic.jsonl", "single_candidate_clean.jsonl", "key_fields_and_status.jsonl"]
    datasets = available if args.dataset == "all" else [args.dataset]
    results = {}
    for dataset in datasets:
        rows = load_rows(archive, dataset)
        evaluated = []
        for row in rows:
            if args.mode == "rules":
                candidates = extractor.extract_rule_candidates(row["case_id"], row["input"]["text"], row["input"].get("classification"))
            else:
                candidates = extractor.extract_llm_candidates(
                    row["case_id"],
                    row["input"]["text"],
                    row["input"].get("classification"),
                    hints=extractor._rule_hints(row["case_id"], row["input"]["text"], row["input"].get("classification")),
                )
            evaluated.append(
                {
                    "case_id": row["case_id"],
                    "text": row["input"]["text"],
                    "raw_gold": row.get("expected_output", {}).get("candidates", []),
                    "gold": gold_candidates(row),
                    "pred": [predicted(candidate) for candidate in candidates],
                }
            )
        results[dataset] = {
            "cases": len(evaluated),
            "candidate": candidate_metrics(evaluated),
            "fields": field_metrics(evaluated),
            "should_store": should_store_metrics(evaluated),
        }
    missing = ["multi_candidate.jsonl", "hard_language_and_noise.jsonl"]
    output = {
        "version": "layer1-metrics-v1",
        "mode": args.mode,
        "generated_at": datetime.now().astimezone().isoformat(),
        "datasets": results,
        "missing_datasets": missing,
    }
    out_dir = root / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"layer1_regression_{args.mode}_{stamp}.json"
    md_path = out_dir / f"layer1_regression_{args.mode}_{stamp}.md"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    lines = [
        "# Layer 1 回归评测",
        "",
        f"模式：`{args.mode}`；样本只读，不写入 memory。",
        "",
        "| 数据集 | Cases | Should-store F1 | Candidate F1 | Type Macro-F1 | Key-field Accuracy | All-fields Exact |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for dataset, result in results.items():
        fields = result["fields"]
        all_exact = fields["all_fields_exact_count"] / fields["gold_candidates"] if fields["gold_candidates"] else 0.0
        lines.append(
            f"| {dataset} | {result['cases']} | {result['should_store']['should_store_f1']:.2%} | {result['candidate']['candidate_f1']:.2%} | {result['candidate']['memory_type_macro_f1']:.2%} | {fields['key_field_accuracy']:.2%} | {all_exact:.2%} |"
        )
    lines += ["", "缺失数据集：`multi_candidate`、`hard_language_and_noise`（仓库压缩包未提供，未虚构结果）。", "", f"JSON：`{json_path}`"]
    md_path.write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            {"json": str(json_path), "markdown": str(md_path), "datasets": results, "missing_datasets": missing},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
