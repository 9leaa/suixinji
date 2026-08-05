"""Append requested detailed Layer 1 / Layer 2 metrics from final saved outputs."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path("/home/zcj/suixinji")
sys.path.insert(0, str(ROOT))
from eval.layer1.run_regression import FIELDS, TYPES, equal, field_metrics  # noqa: E402

MARKER = "## 附录：详细评测指标"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


def prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0, "tp": tp, "fp": fp, "fn": fn}


def candidate_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # Same candidate contract as Layer 1 runner: its stored TP/FP/FN use match()
    # constrained by memory type, so this reproduces the authoritative score.
    from eval.layer1.run_regression import match
    tp = fp = fn = 0
    for row in rows:
        pairs, extra = match(row["gold"], row["pred"])
        tp += sum(pred is not None for _, pred in pairs)
        fn += sum(pred is None for _, pred in pairs)
        fp += len(extra)
    return prf(tp, fp, fn)


def align_types(gold: list[dict[str, Any]], pred: list[dict[str, Any]]) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], int, int]:
    """Align semantic identities while deliberately ignoring memory_type.

    This is solely for a type confusion matrix. Candidate detection's official
    TP/FP/FN remains the type-aware contract above.
    """
    remaining = list(pred)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in gold:
        def score(other: dict[str, Any]) -> int:
            if item.get("evidence_span") and equal(item.get("evidence_span"), other.get("evidence_span")):
                return 3
            if item.get("canonical_topic") and equal(item.get("canonical_topic"), other.get("canonical_topic")):
                return 2
            if item.get("memory_key") and equal(item.get("memory_key"), other.get("memory_key")):
                return 1
            return 0
        ranked = sorted(enumerate(remaining), key=lambda pair: (score(pair[1]), -pair[0]), reverse=True)
        if ranked and score(ranked[0][1]) > 0:
            index, other = ranked[0]
            pairs.append((item, other)); remaining.pop(index)
    return pairs, len(gold) - len(pairs), len(remaining)


def type_confusion(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matrix = {gold: {pred: 0 for pred in TYPES} for gold in TYPES}
    unmatched_gold = unmatched_pred = 0
    for row in rows:
        pairs, missing, extra = align_types(row["gold"], row["pred"])
        unmatched_gold += missing; unmatched_pred += extra
        for gold, pred in pairs:
            g, p = gold.get("memory_type"), pred.get("memory_type")
            if g in matrix and p in matrix[g]: matrix[g][p] += 1
    return {"matrix": matrix, "unmatched_gold": unmatched_gold, "unmatched_pred": unmatched_pred}


def count_exact(rows: list[dict[str, Any]]) -> dict[str, Any]:
    exact = sum(len(row["gold"]) == len(row["pred"]) for row in rows)
    return {"cases": len(rows), "exact": exact, "accuracy": exact / len(rows) if rows else 0.0}


def tag_buckets(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for tag in row.get("coverage_tags") or []: buckets[str(tag)].append(row)
    return {tag: candidate_stats(items) | {"cases": len(items)} for tag, items in sorted(buckets.items())}


def table(lines: list[str], title: str, matrix: dict[str, dict[str, int]], labels: list[str], extra: str = "") -> None:
    lines += [f"#### {title}", "", "Gold \\ Predict | " + " | ".join(labels) + " |", "|---|" + "|".join("---:" for _ in labels) + "|"]
    for gold in labels:
        lines.append("| " + gold + " | " + " | ".join(str(matrix.get(gold, {}).get(pred, 0)) for pred in labels) + " |")
    if extra: lines += ["", extra]
    lines.append("")


def transition_confusion(cases: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    labels = ["todo", "done", "other"]
    matrix = {gold: {pred: 0 for pred in labels} for gold in labels}
    for case in cases:
        for gold, pred in zip(case["gold"].get("decisions", []), case.get("predicted_decisions", [])):
            if gold.get("final_task_status") is None:
                continue
            normalize = lambda value: {"in_progress": "todo", "blocked": "todo", "cancelled": "done", "canceled": "done"}.get(str(value or "other"), str(value or "other"))
            g, p = normalize(gold.get("final_task_status")), normalize(pred.get("final_task_status"))
            matrix[g if g in matrix else "other"][p if p in labels else "other"] += 1
    return matrix


def main() -> None:
    output = Path(sys.argv[1])
    mode_rows = {mode: read_jsonl(output / f"layer1_{mode}" / "cases.jsonl") for mode in ("rules", "hybrid")}
    metrics = {mode: json.loads((output / f"layer1_{mode}" / "metrics.json").read_text(encoding="utf-8")) for mode in mode_rows}
    layer2_cases = read_jsonl(output / "layer2_postgres" / "all" / "predictions.jsonl")
    layer2 = json.loads((output / "layer2_postgres" / "all" / "metrics.json").read_text(encoding="utf-8"))
    extra: dict[str, Any] = {"layer1": {}, "layer2": {}}
    lines = [MARKER, "", "以下均由本次最终保存的逐 case 输出重算；Hybrid 已采用补发覆盖后的最终结果。", ""]

    lines += ["### Layer 1", "", "#### 1. 每数据集 Candidate P/R/F1 与 TP/FP/FN", "", "| Dataset | Mode | TP | FP | FN | Precision | Recall | F1 |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for mode, rows in mode_rows.items():
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows: grouped[str(row["dataset"])].append(row)
        extra["layer1"][mode] = {"candidate": {}, "fields": {}, "type_confusion": {}, "hard_language_tags": {}}
        for dataset, items in sorted(grouped.items()):
            stat = candidate_stats(items); extra["layer1"][mode]["candidate"][dataset] = stat
            lines.append(f"| {dataset} | {mode} | {stat['tp']} | {stat['fp']} | {stat['fn']} | {pct(float(stat['precision']))} | {pct(float(stat['recall']))} | {pct(float(stat['f1']))} |")
            extra["layer1"][mode]["fields"][dataset] = field_metrics(items)["accuracy"]
            extra["layer1"][mode]["type_confusion"][dataset] = type_confusion(items)
            if dataset == "hard_language_and_noise": extra["layer1"][mode]["hard_language_tags"] = tag_buckets(items)
        lines.append("")

    lines += ["#### 2. 四类 Memory Type 混淆矩阵（全 Layer 1）", "", "对 evidence/topic/key 对齐后的候选统计类型；未对齐候选不放入四分类矩阵，另行列出。官方 Candidate P/R/F1 仍使用 type-aware 匹配。", ""]
    for mode, rows in mode_rows.items():
        detail = type_confusion(rows); extra["layer1"][mode]["all_type_confusion"] = detail
        table(lines, mode, detail["matrix"], TYPES, f"未对齐：Gold {detail['unmatched_gold']}，Predict {detail['unmatched_pred']}。")

    lines += ["#### 3. 逐字段 Accuracy（全 Layer 1）", "", "| Field | Rules | Hybrid |", "|---|---:|---:|"]
    for field in FIELDS:
        lines.append(f"| {field} | {pct(metrics['rules']['all']['fields']['accuracy'][field])} | {pct(metrics['hybrid']['all']['fields']['accuracy'][field])} |")
    lines += ["", "#### 4. `multi_candidate` Count Exact", "", "定义：case 内预测候选数与 Gold 候选数完全相等。", "", "| Mode | Exact / Cases | Accuracy |", "|---|---:|---:|"]
    for mode, rows in mode_rows.items():
        value = count_exact([row for row in rows if row["dataset"] == "multi_candidate"]); extra["layer1"][mode]["multi_candidate_count_exact"] = value
        lines.append(f"| {mode} | {value['exact']} / {value['cases']} | {pct(value['accuracy'])} |")

    lines += ["", "#### 5. `hard_language_and_noise` 按 coverage tag 分桶", "", "一个 case 可属于多个 tag，因此各桶可重叠。", "", "| Tag | Mode | Cases | TP | FP | FN | P | R | F1 |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for mode in ("rules", "hybrid"):
        for tag, stat in extra["layer1"][mode]["hard_language_tags"].items():
            lines.append(f"| {tag} | {mode} | {stat['cases']} | {stat['tp']} | {stat['fp']} | {stat['fn']} | {pct(float(stat['precision']))} | {pct(float(stat['recall']))} | {pct(float(stat['f1']))} |")

    identity, source, pending = layer2["task_identity"], layer2["source_link"], layer2["pending_review"]
    extra["layer2"] = {"task_identity": identity, "current_state_accuracy": layer2["current_state_field_accuracy"], "source_link": source, "pending_review": pending, "relation_confusion": layer2["relation_confusion"], "action_confusion": layer2["action_confusion"], "task_transition_confusion": transition_confusion(layer2_cases)}
    lines += ["", "### Layer 2", "", "#### 6. Task Identity P/R/F1", "", "| TP | FP | FN | Precision | Recall | F1 |", "|---:|---:|---:|---:|---:|---:|", f"| {identity['tp']} | {identity['fp']} | {identity['fn']} | {pct(identity['precision'])} | {pct(identity['recall'])} | {pct(identity['f1'])} |", "", "#### 7. Current State Accuracy", "", "| Field | Accuracy |", "|---|---:|"]
    for field, value in layer2["current_state_field_accuracy"].items(): lines.append(f"| {field} | {pct(value)} |")
    lines += ["", "#### 8. Source Link P/R/F1", "", "| TP | FP | FN | Precision | Recall | F1 |", "|---:|---:|---:|---:|---:|---:|", f"| {source['tp']} | {source['fp']} | {source['fn']} | {pct(source['precision'])} | {pct(source['recall'])} | {pct(source['f1'])} |", "", "#### 9. Pending-review P/R/F1", "", "| TP | FP | FN | TN | Precision | Recall | F1 |", "|---:|---:|---:|---:|---:|---:|---:|", f"| {pending['tp']} | {pending['fp']} | {pending['fn']} | {pending['tn']} | {pct(pending['precision'])} | {pct(pending['recall'])} | {pct(pending['f1'])} |", ""]
    table(lines, "10a. Relation 混淆矩阵", layer2["relation_confusion"], ["new", "same", "merge", "update", "supersede", "conflict", "other"])
    table(lines, "10b. Action 混淆矩阵", layer2["action_confusion"], ["insert", "add_source", "update", "pending_review", "other"])
    table(lines, "10c. Task Transition 混淆矩阵（Gold/Pred final_task_status）", extra["layer2"]["task_transition_confusion"], ["todo", "done", "other"])

    report = output / "MEMORY_EVALUATION_EXPERIMENT_REPORT.md"
    original = report.read_text(encoding="utf-8")
    if MARKER in original: original = original.split(MARKER, 1)[0].rstrip() + "\n\n"
    report.write_text(original + "\n".join(lines) + "\n", encoding="utf-8")
    (output / "additional_metrics.json").write_text(json.dumps(extra, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
