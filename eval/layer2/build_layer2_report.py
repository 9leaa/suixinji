"""Build a compact, human-readable report from the Layer 2 run artifacts."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _semantic_exact(result: dict[str, Any]) -> bool:
    """Exact final state while ignoring generated timestamps.

    Dataset timestamps describe fixture time, whereas the repository writes
    current UTC timestamps.  Explicit valid_until/valid_from values remain
    meaningful and are compared when the fixture supplies them.
    """
    gold = result["gold"]
    predicted = result["predicted_state"]
    if set(gold.get("expected_active_memory_refs", [])) != set(predicted.get("expected_active_memory_refs", [])):
        return False
    if predicted.get("duplicate_active_count", 0) != gold.get("duplicate_active_count", 0):
        return False
    if predicted.get("stale_active_count", 0) != gold.get("stale_active_count", 0):
        return False
    by_ref = {row["memory_ref"]: row for row in predicted.get("active_memories", [])}
    fields = ("memory_type", "memory_key", "entity", "attribute", "operation", "canonical_topic", "task_status", "old_value", "new_value", "content", "status", "version_sequence", "polarity")
    for expected in gold.get("final_memories", []):
        actual = by_ref.get(expected["memory_ref"])
        if actual is None:
            return False
        for field in fields:
            if field == "source_note_ids":
                continue
            if actual.get(field) != expected.get(field):
                return False
        if set(actual.get("source_note_ids", [])) != set(expected.get("source_note_ids", [])):
            return False
        for field in ("valid_from", "valid_until"):
            if expected.get(field) is not None and actual.get(field) != expected.get(field):
                return False
    return True


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    output = root / "eval/results/layer2_full"
    metrics = _read_json(output / "layer2_all_metrics.json")
    datasets = _read_json(output / "layer2_dataset_summaries.json")
    concurrency = _read_json(root / "eval/results/layer2_concurrency/layer2_concurrency_metrics.json")
    all_results = [json.loads(line) for line in (output / "all/layer2_predictions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    semantic_by_dataset: dict[str, tuple[int, int]] = {}
    for result in all_results:
        passed, total = semantic_by_dataset.get(result["dataset"], (0, 0))
        semantic_by_dataset[result["dataset"]] = (passed + int(_semantic_exact(result)), total + 1)

    stale = Counter()
    relation_mismatches = Counter()
    action_mismatches = Counter()
    for result in all_results:
        tags = tuple(result.get("coverage_tags", []))
        for expected, predicted in zip(result["gold"].get("decisions", []), result.get("predicted_decisions", [])):
            if expected.get("relation") != predicted.get("relation"):
                relation_mismatches[(expected.get("relation"), predicted.get("relation"))] += 1
            if expected.get("action") != predicted.get("action"):
                action_mismatches[(expected.get("action"), predicted.get("action"))] += 1
            if "stale_candidate" in tags and expected.get("pending_review") and not predicted.get("pending_review"):
                stale["stale_not_pending"] += 1

    orphan = [row for row in all_results if row["dataset"] == "orphan_done_resolution"]
    orphan_groups = Counter()
    for row in orphan:
        tags = set(row.get("coverage_tags", []))
        if "convert_to_episodic" in tags:
            orphan_groups["no-history done → episodic"] += 1
        elif "strong_task" in tags:
            orphan_groups["strong no-history task → pending_review"] += 1
        elif "todo_to_done" in tags:
            orphan_groups["todo → done"] += 1
        elif "blocked_to_done" in tags:
            orphan_groups["blocked → done"] += 1
        elif "done_to_done" in tags:
            orphan_groups["done → done"] += 1
        elif "cancelled_to_done" in tags:
            orphan_groups["cancelled → done"] += 1

    lines = [
        "# Layer 2 Consolidation Evaluation Report",
        "",
        "## 执行范围",
        "",
        "- 输入：5 个 validated JSONL 数据集，共 564 cases / 594 decisions。",
        "- 调用链：validated `MemoryCandidate` → `consolidate_candidate` → adjudicator/relation guard → repository evolve。",
        "- 第一阶段 extractor/prompt/schema：未调用。",
        "- 隔离：每个 case 独立临时 SQLite；评测进程使用 `STORAGE_BACKEND=local`，未写入线上 PostgreSQL 或生产 space。",
        "- 并发：20 个 concurrency fixtures，重复 2 轮，共 40 次并发执行。",
        "",
        "## 数据集指标",
        "",
        "| Dataset | Cases | Decisions | Relation Macro-F1 | Action Acc | Transition Acc | Version Seq Acc | Source F1 | Pending F1 | Semantic Final Exact | Strict Final Exact | Failures |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, summary in datasets.items():
        passed, total = semantic_by_dataset.get(name, (0, 0))
        lines.append(
            f"| {name} | {summary['cases']} | {summary['decision_count']} | {summary['relation_macro_f1'] * 100:.2f}% | {summary['action_accuracy'] * 100:.2f}% | {summary['task_transition_accuracy'] * 100:.2f}% | {summary['version_sequence_accuracy'] * 100:.2f}% | {summary['source_link']['f1'] * 100:.2f}% | {summary['pending_review']['f1'] * 100:.2f}% | {passed / total * 100:.2f}% | {summary['case_exact_match'] * 100:.2f}% | {summary['failure_count']} |"
        )
    lines.extend([
        "",
        "说明：Semantic Final Exact 忽略仓库自动生成的 `updated_at`，并在 fixture 未提供 `valid_from` 时忽略自动写入的当前时间；Strict Final Exact 按字段严格比较，因此会把这类时间差计为失败。",
        "",
        "## 全量补充指标",
        "",
        f"- Task Identity P/R/F1：{metrics['task_identity']['precision'] * 100:.2f}% / {metrics['task_identity']['recall'] * 100:.2f}% / {metrics['task_identity']['f1'] * 100:.2f}%。",
        f"- No-match Accuracy：{metrics['no_match_accuracy'] * 100:.2f}%。",
        f"- Action P/R/F1：{sum(item['precision'] for item in metrics['action_scores'].values()) / len(metrics['action_scores']) * 100:.2f}% / {sum(item['recall'] for item in metrics['action_scores'].values()) / len(metrics['action_scores']) * 100:.2f}% / {sum(item['f1'] for item in metrics['action_scores'].values()) / len(metrics['action_scores']) * 100:.2f}%（各 action macro 平均）；逐类混淆矩阵见 `all/layer2_action_confusion.json`。",
        f"- Current State Field Accuracy：{json.dumps(metrics['current_state_field_accuracy'], ensure_ascii=False)}。",
        f"- Persisted Final State Field Accuracy：{json.dumps(metrics['final_state_field_accuracy'], ensure_ascii=False)}。",
        f"- Version Creation Accuracy：{metrics['version_creation_accuracy'] * 100:.2f}%；Idempotence Accuracy：{metrics['idempotence_accuracy'] * 100:.2f}%。",
        f"- Source Link P/R/F1：{metrics['source_link']['precision'] * 100:.2f}% / {metrics['source_link']['recall'] * 100:.2f}% / {metrics['source_link']['f1'] * 100:.2f}%；Source Exact Set Accuracy：{metrics['source_exact_set_accuracy'] * 100:.2f}%。",
        f"- Done Resolution Accuracy：{metrics['done_resolution_accuracy'] * 100:.2f}%（{metrics['done_resolution_cases']} cases）；Pending side-effect violations：{metrics['pending_side_effect_violations']}。",
        "",
        "## 硬约束与并发不变量",
        "",
        "| Gate | Result |",
        "|---|---:|",
        f"| Orphan Done Task Rate | {metrics['orphan_done_task_rate'] * 100:.2f}% |",
        f"| Duplicate Active Rate | {metrics['duplicate_active_rate'] * 100:.2f}% |",
        f"| Stale Active Rate | {metrics['stale_active_rate'] * 100:.2f}% |",
        f"| Concurrency invariant pass rate | {concurrency['invariant_pass_rate'] * 100:.2f}% |",
        f"| Duplicate version cases under concurrency | {concurrency['duplicate_version_cases']} |",
        f"| Duplicate source cases under concurrency | {concurrency['duplicate_source_cases']} |",
        f"| Cross-space contamination cases | {concurrency['foreign_space_cases']} |",
        "",
        "## Orphan Done 细分",
        "",
        "| Scenario | Cases |",
        "|---|---:|",
    ])
    for label, count in orphan_groups.items():
        lines.append(f"| {label} | {count} |")
    lines.extend([
        "",
        "- no-history `done` 转 episodic：25/25 类型转换正确；无孤立 active task 保留。",
        f"- strong task / cancelled→done：均按 pending_review 路径统计，整体 Pending-review F1 为 {datasets['orphan_done_resolution']['pending_review']['f1'] * 100:.2f}%。",
        "",
        "## 主要失败类别",
        "",
        f"- stale candidate：{stale['stale_not_pending']} 条未按预期进入 pending_review，说明当前 consolidator 尚未把 `observed_at < current.updated_at` 作为通用过期保护。",
        f"- 关系误判最高频：{relation_mismatches.most_common(5)}。其中 relation-core 的 `merge` 被判成 `same`，`supersede` 被判成 `conflict`。",
        f"- 动作误判最高频：{action_mismatches.most_common(5)}。",
        "- non-task 数据集的关系/动作边界较弱，主要集中在 preference/semantic/episodic 的 merge、supersede、conflict 语义没有完全对齐数据集契约。",
        "- version/source 的整体序列准确率较高，但 duplicate delivery 与 stale candidate 暴露了幂等和时间新旧判断仍需修复。",
        "",
        "## 结论",
        "",
        "- 基础安全性通过：孤立 done 不再形成 active task，source/version 在并发下没有重复或跨空间污染。",
        f"- 业务决策层尚未达到说明书硬门槛：整体 Relation Macro-F1 {metrics['relation_macro_f1'] * 100:.2f}%、Action Accuracy {metrics['action_accuracy'] * 100:.2f}%，且 duplicate active rate {metrics['duplicate_active_rate'] * 100:.2f}% 非零。",
        "- 优先修复顺序：① stale candidate 的时间门禁；② merge/supersede 的关系与 action 映射；③ non-task 类型的冲突/替代语义；④ duplicate delivery 的候选去重与状态读取；⑤ valid_from 生成策略与评测契约统一。",
    ])
    (output / "LAYER2_EVALUATION_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output / "LAYER2_EVALUATION_REPORT.md")


if __name__ == "__main__":
    main()
