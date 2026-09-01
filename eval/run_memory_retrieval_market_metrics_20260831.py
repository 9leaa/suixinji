"""Evaluate current Memory retrieval in one shared, isolated PostgreSQL pool.

The runner reuses Layer3 qrels, calls no answer LLM, captures production
retrieval traces, calculates standard IR/channel metrics, and removes its test
space after a successful run.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.query_agent import memory_history
from core.settings import MEMORY_QUERY_MIN_SCORE
from eval.layer3.run_layer3_eval import (
    _candidate_for,
    _complete_seed_memory_vectors,
    _fixture_version_task_status,
    _iso_db,
    load_cases_with_manifest,
)
from infrastructure.schema import MemoryVersion, Note, Space
from memory.service import (
    _coverage_rerank_memory_results,
    _memory_is_non_exportable,
    build_memory_query_spec,
)
from repositories.postgres.common import ensure_tenant_space
from repositories.postgres.memory import _dt, _insert_memory, search_memories, session_scope
from sqlalchemy import select


MEMORY_FIELDS = (
    "memory_type", "status", "task_status", "content", "canonical_topic",
    "entity", "attribute", "current_value", "polarity", "sensitivity",
    "access_scope", "updated_at",
)
VERSION_FIELDS = ("sequence", "content", "status", "task_status", "valid_from", "valid_until")
MEMORY_TYPES = {"task", "preference", "semantic", "episodic"}
CUTOFFS = (1, 3, 5, 10)


def stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def memory_signature(raw: dict[str, Any], versions: list[dict[str, Any]]) -> str:
    return stable({
        "memory": {field: raw.get(field) for field in MEMORY_FIELDS},
        "versions": [
            {field: version.get(field) for field in VERSION_FIELDS}
            for version in sorted(versions, key=lambda item: int(item.get("sequence") or 1))
        ],
    })


def version_signature(parent: str, raw: dict[str, Any]) -> str:
    return stable({"parent": parent, "version": {field: raw.get(field) for field in VERSION_FIELDS}})


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


def rank_metrics(ranked: list[str], grades: dict[str, float]) -> dict[str, float]:
    relevant = {item for item, grade in grades.items() if grade > 0}
    result: dict[str, float] = {}
    for cutoff in CUTOFFS:
        found = len(relevant & set(ranked[:cutoff]))
        result[f"hit_at_{cutoff}"] = float(found > 0)
        result[f"recall_at_{cutoff}"] = found / len(relevant) if relevant else 0.0
        result[f"precision_at_{cutoff}"] = found / cutoff
    first = next((index for index, item in enumerate(ranked, 1) if item in relevant), None)
    result["mrr"] = 1.0 / first if first else 0.0
    found = 0
    ap = 0.0
    for index, item in enumerate(ranked[:10], 1):
        if item in relevant:
            found += 1
            ap += found / index
    result["ap_at_10"] = ap / min(len(relevant), 10) if relevant else 0.0
    dcg = sum(
        (2 ** float(grades.get(item, 0.0)) - 1) / math.log2(index + 1)
        for index, item in enumerate(ranked[:10], 1)
    )
    ideal = sum(
        (2 ** grade - 1) / math.log2(index + 1)
        for index, grade in enumerate(sorted(grades.values(), reverse=True)[:10], 1)
    )
    result["ndcg_at_10"] = dcg / ideal if ideal else 0.0
    return result


def mean_metrics(rows: list[dict[str, float]]) -> dict[str, float]:
    keys = [
        *(f"hit_at_{cutoff}" for cutoff in CUTOFFS),
        *(f"recall_at_{cutoff}" for cutoff in CUTOFFS),
        *(f"precision_at_{cutoff}" for cutoff in CUTOFFS),
        "mrr", "ap_at_10", "ndcg_at_10",
    ]
    if not rows:
        return {key: 0.0 for key in keys}
    return {key: round(statistics.fmean(float(row[key]) for row in rows), 6) for key in keys}


def primary_type(case: dict[str, Any], raw_by_ref: dict[str, dict[str, Any]]) -> str | None:
    expected = case.get("expected") or {}
    refs = [
        *[str(item) for item in expected.get("relevant_current_refs") or []],
        *[str(item) for item in expected.get("relevant_history_refs") or []],
    ]
    types = {
        str(raw_by_ref[ref].get("memory_type"))
        for ref in refs if ref in raw_by_ref and str(raw_by_ref[ref].get("memory_type")) in MEMORY_TYPES
    }
    if len(types) == 1:
        return next(iter(types))
    tags = MEMORY_TYPES & {str(item) for item in case.get("coverage_tags") or []}
    return next(iter(tags)) if len(tags) == 1 else None


class SharedPool:
    def __init__(self, cases: list[dict[str, Any]], run_id: str):
        self.cases = cases
        self.run_id = run_id
        self.space_id = f"memory_ir_eval_{run_id}"
        self.raw_by_signature: dict[str, dict[str, Any]] = {}
        self.versions_by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.case_memory_signature: dict[tuple[str, str], str] = {}
        self.case_version_signature: dict[tuple[str, str], str] = {}
        self.case_raw_by_ref: dict[str, dict[str, dict[str, Any]]] = {}
        self.memory_db_by_signature: dict[str, str] = {}
        self.signature_by_db: dict[str, str] = {}
        self.version_db_by_signature: dict[str, str] = {}
        self.collect()

    def collect(self) -> None:
        for case in self.cases:
            case_id = str(case["case_id"])
            snapshot = (case.get("input") or {}).get("memory_snapshot") or {}
            versions_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for version in snapshot.get("versions") or []:
                versions_by_ref[str(version.get("memory_ref") or "")].append(version)
            raw_by_ref = {str(raw.get("memory_ref") or ""): raw for raw in snapshot.get("memories") or []}
            self.case_raw_by_ref[case_id] = raw_by_ref
            for memory_ref, raw in raw_by_ref.items():
                versions = versions_by_ref.get(memory_ref, [])
                signature = memory_signature(raw, versions)
                self.raw_by_signature.setdefault(signature, raw)
                self.case_memory_signature[(case_id, memory_ref)] = signature
                existing = {version_signature(signature, item) for item in self.versions_by_signature[signature]}
                for version in versions:
                    signature_v = version_signature(signature, version)
                    if signature_v not in existing:
                        self.versions_by_signature[signature].append(version)
                        existing.add(signature_v)
                    self.case_version_signature[(case_id, str(version.get("version_ref") or ""))] = signature_v

    def seed(self) -> dict[str, Any]:
        vector_ids: list[str] = []
        with session_scope() as session:
            ensure_tenant_space(
                session, self.space_id, tenant_id="default", source="memory_ir_eval",
                metadata={"run_id": self.run_id, "isolated": True},
            )
            for index, signature in enumerate(sorted(self.raw_by_signature), 1):
                raw = self.raw_by_signature[signature]
                memory_id = f"mir_{self.run_id}_{index:04d}"
                note_id = f"mir_note_{self.run_id}_{index:04d}"
                created_at = _dt(_iso_db(raw.get("updated_at") or datetime.now(timezone.utc).isoformat()))
                session.add(Note(
                    id=note_id, message_id=note_id, tenant_id="default", space_id=self.space_id,
                    created_at=created_at, title="Memory retrieval evaluation source", note_type="other",
                    summary=str(raw.get("content") or ""), text=str(raw.get("content") or ""),
                    metadata_json={"memory_ir_eval": True}, enrichment_status="ready",
                    enrichment_attempts=0, sensitivity="normal",
                ))
                session.flush()
                candidate = _candidate_for(raw, note_id)
                row = _insert_memory(
                    session, self.space_id, candidate, source_note_id=note_id,
                    source_relation="created_from", status=str(raw.get("status") or "active"),
                    memory_id=memory_id,
                    now=_iso_db(str(raw.get("updated_at") or datetime.now(timezone.utc).isoformat())),
                )
                self.memory_db_by_signature[signature] = memory_id
                self.signature_by_db[memory_id] = signature
                if str(raw.get("status") or "active") == "active":
                    vector_ids.append(memory_id)
                versions = sorted(self.versions_by_signature.get(signature, []), key=lambda item: int(item.get("sequence") or 1))
                if versions:
                    first = session.execute(
                        select(MemoryVersion).where(MemoryVersion.memory_id == memory_id, MemoryVersion.version == 1)
                    ).scalar_one_or_none()
                    for version in versions:
                        sequence = int(version.get("sequence") or 1)
                        target = first if sequence == 1 and first is not None else MemoryVersion(
                            id=f"{memory_id}_v{sequence}", memory_id=memory_id,
                            version=sequence, created_at=row.updated_at,
                        )
                        if target is not first:
                            session.add(target)
                        target.content = str(version.get("content") or row.content)
                        target.status = str(version.get("status") or raw.get("status") or "active")
                        target.task_status = _fixture_version_task_status(version, candidate.memory_type)
                        target.confidence = row.confidence
                        target.importance = row.importance
                        target.valid_from = _dt(_iso_db(version.get("valid_from")))
                        target.valid_until = _dt(_iso_db(version.get("valid_until")))
                        target.reason = "memory_ir_eval_seed"
                        target.source_note_id = note_id
                        session.flush()
                        self.version_db_by_signature[version_signature(signature, version)] = str(target.id)
                    row.current_version = max(int(item.get("sequence") or 1) for item in versions)
            session.flush()
        vectors = _complete_seed_memory_vectors(vector_ids)
        return {
            "space_id": self.space_id,
            "unique_memories": len(self.raw_by_signature),
            "active_memories": len(vector_ids),
            "unique_versions": len(self.version_db_by_signature),
            "vectors": vectors,
        }

    def current_refs(self, case: dict[str, Any], field: str) -> list[str]:
        case_id = str(case["case_id"])
        output = []
        for ref in (case.get("expected") or {}).get(field) or []:
            signature = self.case_memory_signature.get((case_id, str(ref)))
            db_id = self.memory_db_by_signature.get(signature or "")
            if db_id:
                output.append(db_id)
        return list(dict.fromkeys(output))

    def history_refs(self, case: dict[str, Any]) -> list[str]:
        case_id = str(case["case_id"])
        output = []
        for ref in (case.get("expected") or {}).get("relevant_history_refs") or []:
            signature = self.case_version_signature.get((case_id, str(ref)))
            db_id = self.version_db_by_signature.get(signature or "")
            if db_id:
                output.append(db_id)
        return list(dict.fromkeys(output))

    def grades(self, case: dict[str, Any]) -> dict[str, float]:
        case_id = str(case["case_id"])
        output: dict[str, float] = {}
        for ref, grade in ((case.get("expected") or {}).get("graded_relevance") or {}).items():
            signature = self.case_memory_signature.get((case_id, str(ref)))
            db_id = self.memory_db_by_signature.get(signature or "")
            if db_id:
                output[db_id] = max(output.get(db_id, 0.0), float(grade))
        for db_id in self.current_refs(case, "relevant_current_refs"):
            output.setdefault(db_id, 1.0)
        return output

    def cleanup(self) -> None:
        with session_scope() as session:
            row = session.execute(select(Space).where(Space.id == self.space_id)).scalar_one_or_none()
            if row is not None:
                session.delete(row)


def run_search(pool: SharedPool, case: dict[str, Any], memory_type: str | None, trace_enabled: bool) -> dict[str, Any]:
    query = str((case.get("input") or {}).get("query") or "")
    access_context = (case.get("input") or {}).get("access_context") or {}
    spec = build_memory_query_spec(query, memory_type=memory_type)
    trace: list[dict[str, Any]] = []
    started = time.perf_counter()
    rows = search_memories(
        pool.space_id, query, memory_type=memory_type, query_spec=spec,
        include_inactive=False, min_score=0.0, limit=40, mark_access=False,
        access_context=access_context, retrieval_trace=trace if trace_enabled else None,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    candidates = [
        {**record.to_dict(), "score": score}
        for record, score in rows if not _memory_is_non_exportable(record)
    ]
    ranked = _coverage_rerank_memory_results(candidates, query=query, limit=10)
    production = _coverage_rerank_memory_results(
        [item for item in candidates if float(item.get("score") or 0.0) >= float(MEMORY_QUERY_MIN_SCORE)],
        query=query, limit=10,
    )
    return {
        "ranked_ids": [str(item.get("id") or "") for item in ranked],
        "production_ranked_ids": [str(item.get("id") or "") for item in production],
        "trace": trace,
        "latency_ms": round(latency_ms, 3),
        "memory_type": memory_type,
        "query_spec": {
            "memory_type": spec.memory_type, "canonical_topic": spec.canonical_topic,
            "family_key": spec.family_key, "time_mode": spec.time_mode,
        },
    }


def evaluate_case(pool: SharedPool, case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["case_id"])
    expected = case.get("expected") or {}
    raw_by_ref = pool.case_raw_by_ref[case_id]
    memory_type = primary_type(case, raw_by_ref)
    typed = run_search(pool, case, memory_type, True)
    untyped = typed if memory_type is None else run_search(pool, case, None, False)
    grades = pool.grades(case)
    relevant = set(grades)
    forbidden = set(pool.current_refs(case, "must_not_return_refs"))
    trace_by_id = {str(item.get("memory_id") or ""): item for item in typed["trace"]}
    channel_rows: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for memory_id, item in trace_by_id.items():
        for channel, rank in (item.get("channel_ranks") or {}).items():
            channel_rows[str(channel)].append((int(rank), memory_id))
    channel_ranked = {
        channel: [memory_id for _rank, memory_id in sorted(rows)]
        for channel, rows in channel_rows.items()
    }
    expected_history = set(pool.history_refs(case))
    history_ids: list[str] = []
    history_latency_ms = 0.0
    if expected_history:
        started = time.perf_counter()
        rows = memory_history(
            pool.space_id, str((case.get("input") or {}).get("query") or ""), limit=10,
            access_context=(case.get("input") or {}).get("access_context") or {},
        )
        history_latency_ms = (time.perf_counter() - started) * 1000
        history_ids = [str(item.get("id") or "") for item in rows]
    top_id = typed["ranked_ids"][0] if typed["ranked_ids"] else ""
    top_raw = pool.raw_by_signature.get(pool.signature_by_db.get(top_id, ""), {})
    gold_raw = [raw_by_ref[str(ref)] for ref in expected.get("relevant_current_refs") or [] if str(ref) in raw_by_ref]
    state_correct = None
    if gold_raw and len({str(item.get("memory_type")) for item in gold_raw}) == 1:
        gold_type = str(gold_raw[0].get("memory_type"))
        if gold_type == "task":
            state_correct = top_id in relevant and any(str(item.get("task_status")) == str(top_raw.get("task_status")) for item in gold_raw)
        elif gold_type == "preference":
            state_correct = top_id in relevant and any(str(item.get("polarity")) == str(top_raw.get("polarity")) for item in gold_raw)
        elif str(expected.get("evidence_mode")) == "current":
            state_correct = top_id in relevant
    return {
        "case_id": case_id, "dataset": case.get("dataset"), "difficulty": case.get("difficulty"),
        "coverage_tags": case.get("coverage_tags") or [], "query": (case.get("input") or {}).get("query"),
        "answer_type": expected.get("answer_type"), "evidence_mode": expected.get("evidence_mode"),
        "memory_type": memory_type, "relevant_ids": sorted(relevant), "forbidden_ids": sorted(forbidden),
        "typed": typed, "untyped": untyped,
        "typed_metrics": rank_metrics(typed["ranked_ids"], grades) if relevant else None,
        "untyped_metrics": rank_metrics(untyped["ranked_ids"], grades) if relevant else None,
        "channel_ranked": channel_ranked,
        "channel_metrics": {channel: rank_metrics(ranked, grades) for channel, ranked in channel_ranked.items()} if relevant else {},
        "must_not_return_violation": bool(forbidden & set(typed["ranked_ids"])),
        "production_must_not_return_violation": bool(forbidden & set(typed["production_ranked_ids"])),
        "state_correct": state_correct,
        "expected_history_ids": sorted(expected_history), "history_ids": history_ids,
        "history_recall_at_10": len(expected_history & set(history_ids[:10])) / len(expected_history) if expected_history else None,
        "history_latency_ms": round(history_latency_ms, 3),
    }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [item for item in results if item["typed_metrics"] is not None]
    typed = mean_metrics([item["typed_metrics"] for item in eligible])
    untyped = mean_metrics([item["untyped_metrics"] for item in eligible])
    channels = sorted({channel for item in eligible for channel in item["channel_ranked"]})
    contribution: Counter[str] = Counter()
    gold_contribution: Counter[str] = Counter()
    top1_support: Counter[str] = Counter()
    unique_rescue: Counter[str] = Counter()
    for item in eligible:
        gold = set(item["relevant_ids"])
        hits = {channel: bool(gold & set(item["channel_ranked"].get(channel, [])[:10])) for channel in channels}
        for channel, hit in hits.items():
            if hit and sum(hits.values()) == 1:
                unique_rescue[channel] += 1
        top1 = item["typed"]["ranked_ids"][0] if item["typed"]["ranked_ids"] else ""
        for row in item["typed"]["trace"]:
            memory_id = str(row.get("memory_id") or "")
            for channel, value in (row.get("channel_scores") or {}).items():
                contribution[str(channel)] += float(value)
                if memory_id in gold:
                    gold_contribution[str(channel)] += float(value)
            if memory_id == top1:
                for channel in (row.get("channel_ranks") or {}):
                    top1_support[str(channel)] += 1
    contribution_total = sum(contribution.values()) or 1.0
    gold_total = sum(gold_contribution.values()) or 1.0
    channel_metrics = {}
    for channel in channels:
        rows = [item["channel_metrics"][channel] for item in eligible if channel in item["channel_metrics"]]
        metric = mean_metrics(rows)
        metric.update({
            "executed_case_count": len(rows),
            "gold_hit_case_count_at_10": sum(bool(set(item["relevant_ids"]) & set(item["channel_ranked"].get(channel, [])[:10])) for item in eligible),
            "unique_rescue_count_at_10": unique_rescue[channel],
            "top1_support_rate": round(top1_support[channel] / len(eligible), 6),
            "all_candidate_rrf_share": round(contribution[channel] / contribution_total, 6),
            "gold_rrf_share": round(gold_contribution[channel] / gold_total, 6),
        })
        channel_metrics[channel] = metric
    rrf_rows = []
    loo_rows: dict[str, list[dict[str, float]]] = {channel: [] for channel in channels}
    for item in eligible:
        grades = {memory_id: 1.0 for memory_id in item["relevant_ids"]}
        scores = {
            str(row.get("memory_id") or ""): sum(float(value) for value in (row.get("channel_scores") or {}).values())
            for row in item["typed"]["trace"]
        }
        rrf_rows.append(rank_metrics([item for item, _ in sorted(scores.items(), key=lambda pair: pair[1], reverse=True)], grades))
        for channel in channels:
            scores_without = {
                str(row.get("memory_id") or ""): sum(float(value) for name, value in (row.get("channel_scores") or {}).items() if str(name) != channel)
                for row in item["typed"]["trace"]
            }
            loo_rows[channel].append(rank_metrics([item for item, _ in sorted(scores_without.items(), key=lambda pair: pair[1], reverse=True)], grades))
    rrf = mean_metrics(rrf_rows)
    leave_one_out = {}
    for channel, rows in loo_rows.items():
        without = mean_metrics(rows)
        leave_one_out[channel] = {
            "without_channel": without,
            "delta_mrr": round(rrf["mrr"] - without["mrr"], 6),
            "delta_ndcg_at_10": round(rrf["ndcg_at_10"] - without["ndcg_at_10"], 6),
            "delta_recall_at_10": round(rrf["recall_at_10"] - without["recall_at_10"], 6),
        }
    by_type = {}
    for name in sorted(MEMORY_TYPES):
        subset = [item for item in eligible if item["memory_type"] == name]
        by_type[name] = {"cases": len(subset), **mean_metrics([item["typed_metrics"] for item in subset])}
    by_dataset = {}
    for name in sorted({str(item["dataset"]) for item in eligible}):
        subset = [item for item in eligible if str(item["dataset"]) == name]
        by_dataset[name] = {"cases": len(subset), **mean_metrics([item["typed_metrics"] for item in subset])}
    state = [item for item in results if item["state_correct"] is not None]
    history = [item for item in results if item["history_recall_at_10"] is not None]
    no_answer = [item for item in results if item["answer_type"] in {"no_answer", "restricted", "clarification"}]
    latencies = [float(item["typed"]["latency_ms"]) for item in results]
    return {
        "eligible_current_cases": len(eligible), "typed_final": typed, "untyped_final": untyped,
        "routing_lift": {key: round(typed[key] - untyped[key], 6) for key in ("mrr", "ndcg_at_10", "recall_at_10")},
        "by_memory_type": by_type, "by_dataset": by_dataset,
        "channel_metrics": channel_metrics, "rrf_only": rrf, "leave_one_out_rrf_only": leave_one_out,
        "state_accuracy": round(statistics.fmean(bool(item["state_correct"]) for item in state), 6) if state else 0.0,
        "state_cases": len(state),
        "history_recall_at_10": round(statistics.fmean(float(item["history_recall_at_10"]) for item in history), 6) if history else 0.0,
        "history_cases": len(history),
        "must_not_return_violation_rate_at_10": round(statistics.fmean(bool(item["must_not_return_violation"]) for item in results), 6),
        "production_must_not_return_violation_rate_at_10": round(statistics.fmean(bool(item["production_must_not_return_violation"]) for item in results), 6),
        "no_answer_case_count": len(no_answer),
        "no_answer_production_empty_rate": round(statistics.fmean(not item["typed"]["production_ranked_ids"] for item in no_answer), 6) if no_answer else 0.0,
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 3), "p50": round(percentile(latencies, .50), 3),
            "p95": round(percentile(latencies, .95), 3), "p99": round(percentile(latencies, .99), 3),
            "max": round(max(latencies, default=0.0), 3),
        },
    }


def pct(value: float) -> str:
    return f"{value:.2%}"


def write_report(path: Path, manifest: dict[str, Any], metrics: dict[str, Any], failures: list[dict[str, Any]]) -> None:
    final = metrics["typed_final"]
    untyped = metrics["untyped_final"]
    latency = metrics["latency_ms"]
    lines = [
        "# 随心记 Memory 检索行业指标评测", "",
        f"- 日期：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 数据：Layer3 共 {manifest['case_count']} 条；统一隔离池 {manifest['seed']['unique_memories']} 条去重 Memory",
        "- 路径：PostgreSQL + pgvector；当前 Hybrid/Weighted RRF；无 Cross-Encoder；不调用回答 LLM",
        "- 隔离：不读取、不修改真实用户 space；成功结束后删除测试 space", "",
        "## 结论", "",
        f"类型已知时 Recall@1/3/5/10={pct(final['recall_at_1'])}/{pct(final['recall_at_3'])}/{pct(final['recall_at_5'])}/{pct(final['recall_at_10'])}，MRR={final['mrr']:.4f}，NDCG@10={final['ndcg_at_10']:.4f}。",
        f"不给类型时 MRR={untyped['mrr']:.4f}、NDCG@10={untyped['ndcg_at_10']:.4f}；差值属于路由信息收益。", "",
        "## 总体排序指标", "",
        "| 模式 | R@1 | R@3 | R@5 | R@10 | P@10 | MRR | MAP@10 | NDCG@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| 类型路由后 | {pct(final['recall_at_1'])} | {pct(final['recall_at_3'])} | {pct(final['recall_at_5'])} | {pct(final['recall_at_10'])} | {pct(final['precision_at_10'])} | {final['mrr']:.4f} | {final['ap_at_10']:.4f} | {final['ndcg_at_10']:.4f} |",
        f"| 不给类型 | {pct(untyped['recall_at_1'])} | {pct(untyped['recall_at_3'])} | {pct(untyped['recall_at_5'])} | {pct(untyped['recall_at_10'])} | {pct(untyped['precision_at_10'])} | {untyped['mrr']:.4f} | {untyped['ap_at_10']:.4f} | {untyped['ndcg_at_10']:.4f} |", "",
        "## 四类 Memory", "",
        "| 类型 | Cases | R@1 | R@3 | R@5 | R@10 | MRR | NDCG@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in metrics["by_memory_type"].items():
        lines.append(f"| {name} | {row['cases']} | {pct(row['recall_at_1'])} | {pct(row['recall_at_3'])} | {pct(row['recall_at_5'])} | {pct(row['recall_at_10'])} | {row['mrr']:.4f} | {row['ndcg_at_10']:.4f} |")
    lines += ["", "## 各通道能力与贡献", "",
              "| 通道 | 执行Cases | 单路R@1 | 单路R@10 | 单路MRR | Gold命中Cases | Unique Rescue | RRF票数占比 | Gold票数占比 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, row in metrics["channel_metrics"].items():
        lines.append(f"| {name} | {row['executed_case_count']} | {pct(row['recall_at_1'])} | {pct(row['recall_at_10'])} | {row['mrr']:.4f} | {row['gold_hit_case_count_at_10']} | {row['unique_rescue_count_at_10']} | {pct(row['all_candidate_rrf_share'])} | {pct(row['gold_rrf_share'])} |")
    lines += ["", "RRF票数占比只是融合结构，不是正确率；要结合单路能力、Unique Rescue 和 Leave-one-out。", "",
              "## Leave-one-out（仅RRF层）", "",
              "| 删除通道 | MRR下降 | NDCG@10下降 | R@10下降 |", "|---|---:|---:|---:|"]
    for name, row in metrics["leave_one_out_rrf_only"].items():
        lines.append(f"| {name} | {row['delta_mrr']:+.4f} | {row['delta_ndcg_at_10']:+.4f} | {pct(row['delta_recall_at_10'])} |")
    lines += ["", "该消融不包含最终确定性规则重排，只用于解释通道。", "",
              "## 状态、安全、历史和性能", "", "| 指标 | 结果 |", "|---|---:|",
              f"| 当前状态/任务状态/偏好极性 Top1 Accuracy | {pct(metrics['state_accuracy'])} ({metrics['state_cases']} cases) |",
              f"| History Version Recall@10 | {pct(metrics['history_recall_at_10'])} ({metrics['history_cases']} cases) |",
              f"| Raw Top10 Must-not-return 违规率 | {pct(metrics['must_not_return_violation_rate_at_10'])} |",
              f"| 生产阈值后 Must-not-return 违规率 | {pct(metrics['production_must_not_return_violation_rate_at_10'])} |",
              f"| No-answer/Restricted/Clarification 空结果率 | {pct(metrics['no_answer_production_empty_rate'])} ({metrics['no_answer_case_count']} cases) |",
              f"| 延迟 Mean/P50/P95/P99 | {latency['mean']:.1f}/{latency['p50']:.1f}/{latency['p95']:.1f}/{latency['p99']:.1f} ms |", "",
              "## 边界", "",
              "- 这是项目内 Layer3 回归集，不是外部公开 Memory 排行榜。",
              "- 统一池比原先每题 1-6 条 Memory 更难；跨 Case 文档没有人工池化复标，P@K/MAP 按未标注即不相关计算。",
              "- 本次只测 Memory 检索、历史工具和规则排序，不测 Planner、Evidence Resolver、最终答案与引用。",
              f"- 严格失败样本：{len(failures)} 条，见 `failures.jsonl`。", "",
              "## 产物", "", "- `manifest.json`", "- `metrics.json`", "- `cases.jsonl`", "- `failures.jsonl`", "- `MEMORY_RETRIEVAL_MARKET_METRICS_REPORT_20260831.md`"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(ROOT / "eval" / "layer3" / "data_v2"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", default=f"memory_ir_{time.strftime('%Y%m%d_%H%M%S')}")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    cases, contract = load_cases_with_manifest(args.data_dir)
    if args.limit > 0:
        cases = cases[:args.limit]
    pool = SharedPool(cases, args.run_id)
    started = datetime.now(timezone.utc).isoformat()
    seed = pool.seed()
    results: list[dict[str, Any]] = []
    checkpoint = output / "cases.inprogress.jsonl"
    succeeded = False
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
            futures = [executor.submit(evaluate_case, pool, case) for case in cases]
            for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
                item = future.result()
                results.append(item)
                with checkpoint.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                if index % 20 == 0 or index == len(futures):
                    print(f"progress={index}/{len(futures)}", flush=True)
        results.sort(key=lambda item: str(item["case_id"]))
        metrics = aggregate(results)
        failures = [item for item in results if (
            item["typed_metrics"] is not None and float(item["typed_metrics"]["recall_at_5"]) < 1.0
        ) or item["must_not_return_violation"] or item["state_correct"] is False or (
            item["history_recall_at_10"] is not None and float(item["history_recall_at_10"]) < 1.0
        )]
        manifest = {
            "run_id": args.run_id, "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
            "case_count": len(cases), "concurrency": args.concurrency, "data_dir": args.data_dir,
            "contract": contract, "seed": seed,
            "production_entry": "search_memories + deterministic coverage rerank",
            "cross_encoder": False, "answer_llm_called": False, "shared_isolated_pool": True,
            "test_space_removed_on_success": True,
        }
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with (output / "cases.jsonl").open("w", encoding="utf-8") as handle:
            for item in results:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        with (output / "failures.jsonl").open("w", encoding="utf-8") as handle:
            for item in failures:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        write_report(output / "MEMORY_RETRIEVAL_MARKET_METRICS_REPORT_20260831.md", manifest, metrics, failures)
        succeeded = True
        print(json.dumps({"output_dir": str(output), "metrics": metrics["typed_final"], "failures": len(failures)}, ensure_ascii=False), flush=True)
    finally:
        if succeeded:
            pool.cleanup()


if __name__ == "__main__":
    main()
