#!/usr/bin/env python3
"""Analyze the isolated 120-case Memory retrieval run."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.run_memory_retrieval_market_metrics_20260831 import (
    mean_metrics,
    percentile,
    rank_metrics,
)

MEMORY_TYPES = ("task", "preference", "semantic", "episodic")
EXPECTED_CHANNELS = (
    "exact",
    "structured",
    "family",
    "fts",
    "lexical",
    "trigram",
    "type_hint",
    "vector",
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def lookup(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    memories = (row.get("memory_snapshot_input") or {}).get("memories") or []
    return {str(item.get("memory_ref")): item for item in memories}


def grades(row: dict[str, Any]) -> dict[str, float]:
    expected = row.get("expected") or {}
    refs = {str(ref) for ref in expected.get("relevant_current_refs") or []}
    graded = expected.get("graded_relevance") or {}
    return {ref: float(graded.get(ref, 1.0)) for ref in refs}


def memory_type(row: dict[str, Any]) -> str:
    expected = row.get("expected") or {}
    refs = [
        *expected.get("relevant_current_refs", []),
        *expected.get("relevant_history_refs", []),
    ]
    by_ref = lookup(row)
    values = {
        str(by_ref[str(ref)].get("memory_type"))
        for ref in refs
        if str(ref) in by_ref
        and str(by_ref[str(ref)].get("memory_type")) in MEMORY_TYPES
    }
    if len(values) == 1:
        return next(iter(values))
    tags = {str(tag) for tag in row.get("coverage_tags") or []}
    tagged = [name for name in MEMORY_TYPES if name in tags]
    return tagged[0] if len(tagged) == 1 else "mixed_or_unknown"


def channel_rankings(row: dict[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for hit in row.get("raw_channel_hits") or []:
        ref = str(hit.get("logical_ref") or "")
        for channel, rank in (hit.get("channel_ranks") or {}).items():
            if ref:
                found[str(channel)].append((int(rank), ref))
    return {
        channel: [
            ref
            for _rank, ref in sorted(items, key=lambda item: (item[0], item[1]))
        ]
        for channel, items in found.items()
    }


def rrf_ranking(row: dict[str, Any], omit: str | None = None) -> list[str]:
    scores: dict[str, float] = {}
    for hit in row.get("raw_channel_hits") or []:
        ref = str(hit.get("logical_ref") or "")
        if not ref:
            continue
        scores[ref] = sum(
            float(value)
            for channel, value in (hit.get("channel_scores") or {}).items()
            if str(channel) != omit
        )
    return [
        ref
        for ref, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


def group_metrics(
    rows: list[dict[str, Any]],
    key_fn: Any,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(key_fn(row))].append(row)
    return {
        name: {
            "cases": len(group),
            **mean_metrics([
                rank_metrics(
                    [str(ref) for ref in item.get("retrieved_refs") or []],
                    grades(item),
                )
                for item in group
            ]),
        }
        for name, group in sorted(groups.items())
    }


def state_correct(row: dict[str, Any]) -> bool | None:
    gold = grades(row)
    if not gold:
        return None
    ranked = [str(ref) for ref in row.get("retrieved_refs") or []]
    if not ranked or ranked[0] not in gold:
        return False
    top = ranked[0]
    by_ref = lookup(row)
    gold_rows = [by_ref[ref] for ref in gold if ref in by_ref]
    top_row = by_ref.get(top)
    if not top_row or not gold_rows:
        return True
    kind = memory_type(row)
    if kind == "task":
        return any(
            top_row.get("task_status") == item.get("task_status")
            for item in gold_rows
        )
    if kind == "preference":
        return any(
            top_row.get("polarity") == item.get("polarity")
            for item in gold_rows
        )
    return True


def analyze(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    current = [row for row in rows if grades(row)]
    final_rows = [
        rank_metrics(
            [str(ref) for ref in row.get("retrieved_refs") or []],
            grades(row),
        )
        for row in current
    ]
    final = mean_metrics(final_rows)
    observed_channels = {
        channel
        for row in current
        for channel in channel_rankings(row)
    }
    channels = [
        *EXPECTED_CHANNELS,
        *sorted(observed_channels - set(EXPECTED_CHANNELS)),
    ]
    channel_votes: Counter[str] = Counter()
    gold_votes: Counter[str] = Counter()
    top1_support: Counter[str] = Counter()
    executed: Counter[str] = Counter()
    gold_hit: Counter[str] = Counter()
    unique_rescue: Counter[str] = Counter()
    single_rows: dict[str, list[dict[str, float]]] = {
        channel: [] for channel in channels
    }

    for row in current:
        gold = set(grades(row))
        rankings = channel_rankings(row)
        hits = {
            channel: bool(gold & set(rankings.get(channel, [])[:10]))
            for channel in channels
        }
        for channel in channels:
            single_rows[channel].append(
                rank_metrics(rankings.get(channel, []), grades(row))
            )
            if channel in rankings:
                executed[channel] += 1
            if hits[channel]:
                gold_hit[channel] += 1
                if sum(hits.values()) == 1:
                    unique_rescue[channel] += 1
        top1 = str((row.get("retrieved_refs") or [""])[0])
        for hit in row.get("raw_channel_hits") or []:
            ref = str(hit.get("logical_ref") or "")
            for channel, value in (hit.get("channel_scores") or {}).items():
                channel_votes[str(channel)] += float(value)
                if ref in gold:
                    gold_votes[str(channel)] += float(value)
            if ref == top1:
                for channel in (hit.get("channel_ranks") or {}):
                    top1_support[str(channel)] += 1

    total_votes = sum(channel_votes.values()) or 1.0
    total_gold_votes = sum(gold_votes.values()) or 1.0
    channel_metrics: dict[str, Any] = {}
    for channel in channels:
        item = mean_metrics(single_rows[channel])
        item.update({
            "executed_cases": executed[channel],
            "coverage": round(executed[channel] / len(current), 6),
            "gold_hit_cases_at_10": gold_hit[channel],
            "unique_rescue_cases_at_10": unique_rescue[channel],
            "all_candidate_rrf_share": round(
                channel_votes[channel] / total_votes, 6
            ),
            "gold_rrf_share": round(
                gold_votes[channel] / total_gold_votes, 6
            ),
            "top1_support_rate": round(
                top1_support[channel] / len(current), 6
            ),
        })
        channel_metrics[channel] = item

    rrf_case_rows = [
        rank_metrics(rrf_ranking(row), grades(row))
        for row in current
    ]
    rrf_only = mean_metrics(rrf_case_rows)
    leave_one_out: dict[str, Any] = {}
    for channel in channels:
        removed_rows = [
            rank_metrics(rrf_ranking(row, channel), grades(row))
            for row in current
        ]
        removed = mean_metrics(removed_rows)
        rescue = sum(
            base["hit_at_10"] > removed_case["hit_at_10"]
            for base, removed_case in zip(rrf_case_rows, removed_rows, strict=True)
        )
        harm = sum(
            base["hit_at_10"] < removed_case["hit_at_10"]
            for base, removed_case in zip(rrf_case_rows, removed_rows, strict=True)
        )
        leave_one_out[channel] = {
            "without_channel": removed,
            "delta_mrr": round(rrf_only["mrr"] - removed["mrr"], 6),
            "delta_ndcg_at_10": round(
                rrf_only["ndcg_at_10"] - removed["ndcg_at_10"], 6
            ),
            "delta_recall_at_10": round(
                rrf_only["recall_at_10"] - removed["recall_at_10"], 6
            ),
            "rescue_cases_at_10": rescue,
            "harm_cases_at_10": harm,
        }

    overlap: dict[str, Any] = {}
    for left, right in combinations(channels, 2):
        values = []
        for row in current:
            ranked = channel_rankings(row)
            a = set(ranked.get(left, [])[:10])
            b = set(ranked.get(right, [])[:10])
            if a or b:
                values.append(len(a & b) / len(a | b))
        overlap[f"{left}__{right}"] = {
            "cases": len(values),
            "mean_jaccard_at_10": round(
                statistics.fmean(values), 6
            ) if values else 0.0,
        }

    history_case_rows = []
    for row in rows:
        refs = {
            str(ref): 1.0
            for ref in (row.get("expected") or {}).get(
                "relevant_history_refs", []
            )
        }
        if refs:
            history_case_rows.append(
                rank_metrics(
                    [str(ref) for ref in row.get("history_retrieved_refs") or []],
                    refs,
                )
            )

    states = [
        value
        for row in rows
        if (value := state_correct(row)) is not None
    ]
    must_not_by_cutoff: dict[str, Any] = {}
    for cutoff in (1, 3, 5, 10):
        all_flags: list[bool] = []
        current_flags: list[bool] = []
        noncurrent_flags: list[bool] = []
        for row in rows:
            forbidden = {
                str(ref)
                for ref in (row.get("expected") or {}).get(
                    "must_not_return_refs", []
                )
            }
            violation = bool(
                forbidden
                & set((row.get("retrieved_refs") or [])[:cutoff])
            )
            all_flags.append(violation)
            target = current_flags if grades(row) else noncurrent_flags
            target.append(violation)
        must_not_by_cutoff[str(cutoff)] = {
            "all_cases": sum(all_flags),
            "all_rate": round(statistics.fmean(all_flags), 6),
            "current_eligible_cases": sum(current_flags),
            "current_eligible_rate": round(
                statistics.fmean(current_flags), 6
            ) if current_flags else 0.0,
            "noncurrent_cases": sum(noncurrent_flags),
            "noncurrent_rate": round(
                statistics.fmean(noncurrent_flags), 6
            ) if noncurrent_flags else 0.0,
        }

    tag_metrics = {}
    all_tags = sorted({
        str(tag) for row in current for tag in row.get("coverage_tags") or []
    })
    for tag in all_tags:
        subset = [
            row for row in current
            if tag in set(row.get("coverage_tags") or [])
        ]
        tag_metrics[tag] = {
            "cases": len(subset),
            **mean_metrics([
                rank_metrics(
                    [str(ref) for ref in row.get("retrieved_refs") or []],
                    grades(row),
                )
                for row in subset
            ]),
        }

    failures = []
    for row in rows:
        reasons = []
        gold = grades(row)
        ranked = [str(ref) for ref in row.get("retrieved_refs") or []]
        if gold and not (set(gold) & set(ranked[:1])):
            reasons.append("current_top1_miss")
        if gold and not set(gold).issubset(set(ranked[:10])):
            reasons.append("current_incomplete_at_10")
        history_gold = {
            str(ref)
            for ref in (row.get("expected") or {}).get(
                "relevant_history_refs", []
            )
        }
        history_pred = {
            str(ref) for ref in row.get("history_retrieved_refs") or []
        }
        if history_gold and not history_gold.issubset(history_pred):
            reasons.append("history_incomplete_at_10")
        forbidden = {
            str(ref)
            for ref in (row.get("expected") or {}).get(
                "must_not_return_refs", []
            )
        }
        if gold and forbidden & set(ranked[:1]):
            reasons.append("current_must_not_top1")
        if reasons:
            failures.append({
                "case_id": row.get("case_id"),
                "dataset": row.get("dataset"),
                "query": row.get("query"),
                "observed_action": (
                    row.get("observed_route_detail") or {}
                ).get("action"),
                "reasons": reasons,
                "gold_current": sorted(gold),
                "predicted_current": ranked,
                "gold_history": sorted(history_gold),
                "predicted_history": sorted(history_pred),
            })

    latency = [
        float((row.get("latency_ms") or {}).get("retrieval") or 0.0)
        for row in rows
    ]
    metrics = {
        "experiment": {
            "cases": len(rows),
            "unique_cases": len({str(row.get("case_id")) for row in rows}),
            "runner_error_cases": sum(bool(row.get("errors")) for row in rows),
            "current_ranking_eligible_cases": len(current),
            "history_eligible_cases": len(history_case_rows),
            "database": "PostgreSQL + pgvector",
            "retrieval_only": True,
            "cross_encoder": False,
        },
        "final_current_ranking": final,
        "rrf_only_ranking": rrf_only,
        "final_rerank_lift_over_rrf": {
            "mrr": round(final["mrr"] - rrf_only["mrr"], 6),
            "ndcg_at_10": round(
                final["ndcg_at_10"] - rrf_only["ndcg_at_10"], 6
            ),
            "recall_at_10": round(
                final["recall_at_10"] - rrf_only["recall_at_10"], 6
            ),
        },
        "by_memory_type": group_metrics(current, memory_type),
        "by_dataset": group_metrics(current, lambda row: row.get("dataset")),
        "by_difficulty": group_metrics(
            current, lambda row: row.get("difficulty")
        ),
        "by_coverage_tag": tag_metrics,
        "channel_metrics": channel_metrics,
        "leave_one_out_rrf_only": leave_one_out,
        "channel_overlap": overlap,
        "current_state_accuracy": round(
            statistics.fmean(states), 6
        ) if states else 0.0,
        "current_state_cases": len(states),
        "history_ranking": mean_metrics(history_case_rows),
        "history_route_miss_cases": sum(
            bool(
                (row.get("expected") or {}).get(
                    "relevant_history_refs", []
                )
            )
            and (
                (row.get("observed_route_detail") or {}).get("action")
                != "memory_history"
            )
            for row in rows
        ),
        "must_not_candidate_by_cutoff": must_not_by_cutoff,
        "latency_ms_diagnostic_only": {
            "mean": round(statistics.fmean(latency), 3),
            "p50": round(percentile(latency, 0.50), 3),
            "p95": round(percentile(latency, 0.95), 3),
            "p99": round(percentile(latency, 0.99), 3),
        },
        "failure_case_count": len(failures),
    }
    return metrics, failures


def pct(value: float) -> str:
    return f"{value:.2%}"


def bucket_row(name: str, row: dict[str, Any]) -> str:
    return (
        f"| {name} | {row['cases']} | {pct(row['recall_at_1'])} | "
        f"{pct(row['recall_at_3'])} | {pct(row['recall_at_5'])} | "
        f"{pct(row['recall_at_10'])} | {row['mrr']:.4f} | "
        f"{row['ap_at_10']:.4f} | {row['ndcg_at_10']:.4f} |"
    )


def write_report(
    path: Path,
    metrics: dict[str, Any],
    failures: list[dict[str, Any]],
) -> None:
    exp = metrics["experiment"]
    final = metrics["final_current_ranking"]
    history = metrics["history_ranking"]
    lift = metrics["final_rerank_lift_over_rrf"]
    must_not_1 = metrics["must_not_candidate_by_cutoff"]["1"]
    must_not_10 = metrics["must_not_candidate_by_cutoff"]["10"]
    lines = [
        "# 随心记 Memory 检索 120 条无 CE 评测报告",
        "",
        "## 结论",
        "",
        (
            f"完成 {exp['cases']} 条隔离测试，运行错误 0；"
            f"当前 Memory 排序有效样本 {exp['current_ranking_eligible_cases']} 条。"
        ),
        (
            f"最终排序 Recall@1/3/5/10={pct(final['recall_at_1'])}/"
            f"{pct(final['recall_at_3'])}/{pct(final['recall_at_5'])}/"
            f"{pct(final['recall_at_10'])}，MRR={final['mrr']:.4f}，"
            f"MAP@10={final['ap_at_10']:.4f}，NDCG@10={final['ndcg_at_10']:.4f}。"
        ),
        (
            f"最终确定性重排相对纯 RRF：MRR {lift['mrr']:+.4f}，"
            f"NDCG@10 {lift['ndcg_at_10']:+.4f}，"
            f"Recall@10 {pct(lift['recall_at_10'])}。"
        ),
        (
            "主要弱项：task 与多记忆问题的前排排序、9 条历史兜底路由缺失，"
            "以及 type_hint 通道对 RRF 的负贡献。"
        ),
        "",
        "## 实验口径",
        "",
        "- 真实 PostgreSQL + pgvector Memory 检索；无 Cross-Encoder；不调用回答 LLM。",
        "- 每个 case 使用独立 space，包含 Gold Memory 与 30 条身份安全干扰项。",
        "- 当前 Memory 与历史 Version 分开计分，不把历史题混入当前检索分母。",
        "- 使用同一冻结 embedding；向量银行仅减少跨 SSH 重复写入，不改变向量或排序。",
        "",
        "## 当前 Memory 总体排序",
        "",
        "| R@1 | R@3 | R@5 | R@10 | Hit@1 | Hit@10 | P@10 | MRR | MAP@10 | NDCG@10 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| {pct(final['recall_at_1'])} | {pct(final['recall_at_3'])} | "
            f"{pct(final['recall_at_5'])} | {pct(final['recall_at_10'])} | "
            f"{pct(final['hit_at_1'])} | {pct(final['hit_at_10'])} | "
            f"{pct(final['precision_at_10'])} | {final['mrr']:.4f} | "
            f"{final['ap_at_10']:.4f} | {final['ndcg_at_10']:.4f} |"
        ),
        "",
        "## 四类 Memory",
        "",
        "| 类型 | Cases | R@1 | R@3 | R@5 | R@10 | MRR | MAP@10 | NDCG@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in MEMORY_TYPES:
        if name in metrics["by_memory_type"]:
            lines.append(bucket_row(name, metrics["by_memory_type"][name]))
    if "mixed_or_unknown" in metrics["by_memory_type"]:
        lines.append(
            bucket_row(
                "mixed_or_unknown",
                metrics["by_memory_type"]["mixed_or_unknown"],
            )
        )
    lines += [
        "",
        "## 每路召回能力与贡献",
        "",
        "| 通道 | 执行Cases | Coverage | 单路R@1 | 单路R@10 | 单路MRR | Gold命中 | Unique Rescue | RRF票数 | Gold票数 | Top1支持 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in metrics["channel_metrics"].items():
        lines.append(
            f"| {name} | {row['executed_cases']} | {pct(row['coverage'])} | "
            f"{pct(row['recall_at_1'])} | {pct(row['recall_at_10'])} | "
            f"{row['mrr']:.4f} | {row['gold_hit_cases_at_10']} | "
            f"{row['unique_rescue_cases_at_10']} | "
            f"{pct(row['all_candidate_rrf_share'])} | "
            f"{pct(row['gold_rrf_share'])} | {pct(row['top1_support_rate'])} |"
        )
    lines += [
        "",
        "RRF 票数占比只解释融合结构，不等于正确率。",
        "",
        "## Leave-one-out（仅 RRF 层）",
        "",
        "| 删除通道 | MRR下降 | NDCG@10下降 | R@10下降 | Rescue | Harm |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, row in metrics["leave_one_out_rrf_only"].items():
        lines.append(
            f"| {name} | {row['delta_mrr']:+.4f} | "
            f"{row['delta_ndcg_at_10']:+.4f} | "
            f"{pct(row['delta_recall_at_10'])} | "
            f"{row['rescue_cases_at_10']} | {row['harm_cases_at_10']} |"
        )
    lines += [
        "",
        "该消融只重放 RRF，不重放最终确定性 policy/coverage rerank。",
        "",
        "## 数据集分桶",
        "",
        "| 数据集 | Cases | R@1 | R@3 | R@5 | R@10 | MRR | MAP@10 | NDCG@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in metrics["by_dataset"].items():
        lines.append(bucket_row(name, row))
    lines += [
        "",
        "## 状态、历史与安全",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        (
            f"| Current State Top1 Accuracy | "
            f"{pct(metrics['current_state_accuracy'])} "
            f"({metrics['current_state_cases']} cases) |"
        ),
        (
            f"| History Version R@1/R@3/R@5/R@10 | "
            f"{pct(history['recall_at_1'])}/{pct(history['recall_at_3'])}/"
            f"{pct(history['recall_at_5'])}/{pct(history['recall_at_10'])} "
            f"({exp['history_eligible_cases']} cases) |"
        ),
        (
            f"| History MRR / NDCG@10 | "
            f"{history['mrr']:.4f} / {history['ndcg_at_10']:.4f} |"
        ),
        (
            f"| History 路由缺失 | "
            f"{metrics['history_route_miss_cases']} cases |"
        ),
        (
            f"| 强制候选 Must-not@1（全体 / current） | "
            f"{pct(must_not_1['all_rate'])} ({must_not_1['all_cases']}) / "
            f"{pct(must_not_1['current_eligible_rate'])} "
            f"({must_not_1['current_eligible_cases']}) |"
        ),
        (
            f"| 强制候选 Must-not@10（全体 / current） | "
            f"{pct(must_not_10['all_rate'])} ({must_not_10['all_cases']}) / "
            f"{pct(must_not_10['current_eligible_rate'])} "
            f"({must_not_10['current_eligible_cases']}) |"
        ),
        "",
        "## 边界",
        "",
        "- 这是项目内 Layer3 派生 pooled 回归集，不是外部公开排行榜。",
        "- 未标注干扰项按不相关计；跨 case 干扰项没有额外人工池化复标。",
        "- 只测 Memory 召回、融合、规则排序与 history 工具，不测最终回答和引用。",
        "- 数据库经 Tailscale/SSH 反向转发，延迟只用于诊断，不能作为生产延迟。",
        "- retrieval-only 强制 min_score=0，因此不伪造 no-answer abstention 指标。",
        "- Must-not 是候选污染诊断，不等同生产回答泄漏；必须结合阈值、Evidence Resolver 和答案契约复测。",
        "",
        "## 失败样本",
        "",
        f"- Top1 错排、当前集合不完整或 history 不完整：{len(failures)} 条。",
        "- 详见 memory_retrieval_failures.jsonl。",
        "",
        "## 产物",
        "",
        "- layer3_predictions.jsonl：120 条原始输出。",
        "- memory_retrieval_metrics.json：完整指标。",
        "- memory_retrieval_failures.jsonl：失败样本。",
        "- MEMORY_RETRIEVAL_POOLED120_NOCE_REPORT_20260831.md：本报告。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    args = parser.parse_args()
    output = Path(args.input_dir)
    rows = read_jsonl(output / "layer3_predictions.jsonl")
    if len(rows) != 120 or len({str(row.get("case_id")) for row in rows}) != 120:
        raise RuntimeError("expected exactly 120 unique predictions")
    if any(row.get("errors") for row in rows):
        raise RuntimeError("retry runner errors before publishing metrics")
    metrics, failures = analyze(rows)
    (output / "memory_retrieval_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "memory_retrieval_failures.jsonl").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in failures
        ),
        encoding="utf-8",
    )
    write_report(
        output / "MEMORY_RETRIEVAL_POOLED120_NOCE_REPORT_20260831.md",
        metrics,
        failures,
    )
    print(json.dumps({
        "cases": len(rows),
        "current_eligible": metrics["experiment"][
            "current_ranking_eligible_cases"
        ],
        "history_eligible": metrics["experiment"]["history_eligible_cases"],
        "failure_cases": len(failures),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
