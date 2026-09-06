"""P1 Ask evaluation: execute public Memory/Task tools on an isolated PostgreSQL pool."""

from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any

ROOT = Path.cwd()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.query_agent import search_memories, search_tasks
import repositories.postgres.memory as postgres_memory
from eval.layer3.run_layer3_eval import (
    _candidate_for,
    _complete_seed_memory_vectors,
    _fixture_version_task_status,
    _iso_db,
    load_cases_with_manifest,
)
from eval.run_memory_retrieval_market_metrics_20260831 import (
    MEMORY_TYPES,
    SharedPool,
    mean_metrics,
    percentile,
    primary_type,
    rank_metrics,
    version_signature,
)
from infrastructure.schema import Memory, MemorySource, MemoryVersion, MemoryVersionSource, Note
from repositories.postgres.common import ensure_tenant_space
from repositories.postgres.memory import _dt, session_scope
from sqlalchemy import insert


def ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row.get("id") or row.get("memory_id") or "") for row in rows if row.get("id") or row.get("memory_id")]


class FastSharedPool(SharedPool):
    """Preserve SharedPool fixture IDs while inserting each table in one batch."""

    def seed(self) -> dict[str, Any]:
        note_rows = []
        memory_rows = []
        source_rows = []
        version_rows = []
        version_source_rows = []
        vector_ids = []
        with session_scope() as session:
            ensure_tenant_space(
                session,
                self.space_id,
                tenant_id="default",
                source="ask_p1_memory_eval",
                metadata={"run_id": self.run_id, "isolated": True},
            )
            for index, signature in enumerate(sorted(self.raw_by_signature), 1):
                raw = self.raw_by_signature[signature]
                memory_id = f"mir_{self.run_id}_{index:04d}"
                note_id = f"mir_note_{self.run_id}_{index:04d}"
                timestamp = _dt(_iso_db(raw.get("updated_at") or datetime.now(timezone.utc).isoformat()))
                candidate = _candidate_for(raw, note_id)
                fixture_versions = sorted(
                    self.versions_by_signature.get(signature, []),
                    key=lambda item: int(item.get("sequence") or 1),
                ) or [
                    {
                        "sequence": 1,
                        "content": candidate.content,
                        "status": str(raw.get("status") or "active"),
                        "task_status": candidate.task_status,
                        "valid_from": raw.get("updated_at"),
                        "valid_until": None,
                    }
                ]
                current_version = max(int(item.get("sequence") or 1) for item in fixture_versions)
                note_rows.append(
                    {
                        "id": note_id,
                        "message_id": note_id,
                        "tenant_id": "default",
                        "space_id": self.space_id,
                        "created_at": timestamp,
                        "title": "Memory retrieval evaluation source",
                        "note_type": "other",
                        "summary": str(raw.get("content") or ""),
                        "text": str(raw.get("content") or ""),
                        "metadata_json": {"memory_ir_eval": True},
                        "enrichment_status": "ready",
                        "enrichment_attempts": 0,
                        "sensitivity": "normal",
                    }
                )
                memory_rows.append(
                    {
                        "id": memory_id,
                        "tenant_id": "default",
                        "space_id": self.space_id,
                        "memory_type": candidate.memory_type,
                        "content": candidate.content,
                        "normalized_content": candidate.normalized_content,
                        "importance": float(candidate.importance),
                        "confidence": float(candidate.confidence),
                        "status": str(raw.get("status") or "active"),
                        "task_status": candidate.task_status,
                        "subject": candidate.subject,
                        "predicate": candidate.predicate,
                        "object_value": candidate.object_value,
                        "memory_key": candidate.effective_memory_key,
                        "memory_key_version": candidate.memory_key_version,
                        "polarity": candidate.polarity,
                        "scope_json": dict(candidate.scope),
                        "valid_from": _dt(candidate.valid_from),
                        "valid_until": _dt(candidate.valid_until),
                        "last_confirmed_at": timestamp,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "last_accessed_at": None,
                        "access_count": 0,
                        "current_version": current_version,
                    }
                )
                source_rows.append({"memory_id": memory_id, "note_id": note_id, "relation": "created_from", "created_at": timestamp})
                self.memory_db_by_signature[signature] = memory_id
                self.signature_by_db[memory_id] = signature
                if str(raw.get("status") or "active") == "active":
                    vector_ids.append(memory_id)
                for raw_version in fixture_versions:
                    sequence = int(raw_version.get("sequence") or 1)
                    version_id = f"{memory_id}_v{sequence}"
                    version_rows.append(
                        {
                            "id": version_id,
                            "memory_id": memory_id,
                            "version": sequence,
                            "content": str(raw_version.get("content") or candidate.content),
                            "status": str(raw_version.get("status") or raw.get("status") or "active"),
                            "task_status": _fixture_version_task_status(raw_version, candidate.memory_type),
                            "confidence": float(candidate.confidence),
                            "importance": float(candidate.importance),
                            "valid_from": _dt(_iso_db(raw_version.get("valid_from"))),
                            "valid_until": _dt(_iso_db(raw_version.get("valid_until"))),
                            "reason": "ask_p1_memory_seed",
                            "source_note_id": note_id,
                            "created_at": timestamp,
                        }
                    )
                    version_source_rows.append({"version_id": version_id, "note_id": note_id, "created_at": timestamp})
                    self.version_db_by_signature[version_signature(signature, raw_version)] = version_id
            session.execute(insert(Note), note_rows)
            session.execute(insert(Memory), memory_rows)
            session.execute(insert(MemorySource), source_rows)
            session.execute(insert(MemoryVersion), version_rows)
            session.execute(insert(MemoryVersionSource), version_source_rows)
        vectors = _complete_seed_memory_vectors(vector_ids)
        return {
            "space_id": self.space_id,
            "unique_memories": len(self.raw_by_signature),
            "active_memories": len(vector_ids),
            "unique_versions": len(version_rows),
            "vectors": vectors,
            "batch_seed": True,
        }


def evaluate_case(pool: SharedPool, case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["case_id"])
    expected = case.get("expected") or {}
    raw_by_ref = pool.case_raw_by_ref[case_id]
    memory_type = primary_type(case, raw_by_ref)
    access = (case.get("input") or {}).get("access_context") or {}
    query = str((case.get("input") or {}).get("query") or "")
    relevant = set(pool.current_refs(case, "relevant_current_refs"))
    forbidden = set(pool.current_refs(case, "must_not_return_refs"))
    history_gold = set(pool.history_refs(case))
    current_rows: list[dict[str, Any]] = []
    history_rows: list[dict[str, Any]] = []
    current_error = None
    history_error = None
    current_ms = 0.0
    history_ms = 0.0

    if memory_type in MEMORY_TYPES:
        started = time.perf_counter()
        try:
            if memory_type == "task":
                current_rows = search_tasks(pool.space_id, query, view="current", limit=10, min_score=0.0, access_context=access)
            else:
                current_rows = search_memories(
                    pool.space_id,
                    query,
                    memory_types=[memory_type],
                    time_scope="current",
                    limit=10,
                    min_score=0.0,
                    access_context=access,
                )
        except Exception as exc:
            current_error = f"{type(exc).__name__}: {exc}"
        current_ms = (time.perf_counter() - started) * 1000

    if history_gold and memory_type in MEMORY_TYPES:
        started = time.perf_counter()
        try:
            if memory_type == "task":
                history_rows = search_tasks(pool.space_id, query, view="timeline", limit=10, min_score=0.0, access_context=access)
            else:
                history_rows = search_memories(
                    pool.space_id,
                    query,
                    memory_types=[memory_type],
                    time_scope="history",
                    limit=10,
                    min_score=0.0,
                    access_context=access,
                )
        except Exception as exc:
            history_error = f"{type(exc).__name__}: {exc}"
        history_ms = (time.perf_counter() - started) * 1000

    ranked = ids(current_rows)
    history_ranked = ids(history_rows)
    grades = pool.grades(case)
    current_metrics = rank_metrics(ranked, grades) if relevant and memory_type in MEMORY_TYPES else None
    top_id = ranked[0] if ranked else ""
    top_raw = pool.raw_by_signature.get(pool.signature_by_db.get(top_id, ""), {})
    gold_raw = [raw_by_ref[str(ref)] for ref in expected.get("relevant_current_refs") or [] if str(ref) in raw_by_ref]
    state_correct = None
    if gold_raw and memory_type == "task":
        state_correct = top_id in relevant and any(str(item.get("task_status")) == str(top_raw.get("task_status")) for item in gold_raw)
    elif gold_raw and memory_type == "preference":
        state_correct = top_id in relevant and any(str(item.get("polarity")) == str(top_raw.get("polarity")) for item in gold_raw)
    elif gold_raw and str(expected.get("evidence_mode")) == "current":
        state_correct = top_id in relevant

    return {
        "case_id": case_id,
        "dataset": case.get("dataset"),
        "difficulty": case.get("difficulty"),
        "memory_type": memory_type,
        "query": query,
        "answer_type": expected.get("answer_type"),
        "current_tool": "search_tasks" if memory_type == "task" else "search_memories" if memory_type else None,
        "current_error": current_error,
        "current_latency_ms": round(current_ms, 3),
        "current_ids": ranked,
        "relevant_ids": sorted(relevant),
        "current_metrics": current_metrics,
        "state_correct": state_correct,
        "forbidden_ids": sorted(forbidden),
        "must_not_violation": bool(forbidden & set(ranked[:10])),
        "history_error": history_error,
        "history_latency_ms": round(history_ms, 3),
        "history_ids": history_ranked,
        "history_gold_ids": sorted(history_gold),
        "history_recall_at_10": len(history_gold & set(history_ranked[:10])) / len(history_gold) if history_gold else None,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if row["current_metrics"] is not None]
    execution = [row for row in rows if row["memory_type"] in MEMORY_TYPES]
    state = [row for row in rows if row["state_correct"] is not None]
    history = [row for row in rows if row["history_recall_at_10"] is not None]
    no_answer = [row for row in execution if row["answer_type"] in {"no_answer", "restricted", "clarification"}]
    latencies = [float(row["current_latency_ms"]) for row in execution]
    by_type = {}
    for name in sorted(MEMORY_TYPES):
        bucket = [row for row in eligible if row["memory_type"] == name]
        by_type[name] = {"cases": len(bucket), **mean_metrics([row["current_metrics"] for row in bucket])}
    return {
        "total_cases": len(rows),
        "single_tool_cases": len(execution),
        "excluded_multi_or_untyped_cases": len(rows) - len(execution),
        "execution_success_rate": round(sum(not row["current_error"] for row in execution) / len(execution), 6),
        "eligible_current_cases": len(eligible),
        "current_retrieval": mean_metrics([row["current_metrics"] for row in eligible]),
        "by_memory_type": by_type,
        "state_accuracy": round(statistics.fmean(bool(row["state_correct"]) for row in state), 6) if state else 0.0,
        "state_cases": len(state),
        "history_execution_success_rate": round(sum(not row["history_error"] for row in history) / len(history), 6) if history else 0.0,
        "history_recall_at_10": round(statistics.fmean(float(row["history_recall_at_10"]) for row in history), 6) if history else 0.0,
        "history_cases": len(history),
        "must_not_violation_rate_at_10": round(statistics.fmean(bool(row["must_not_violation"]) for row in execution), 6),
        "no_answer_empty_rate": round(statistics.fmean(not row["current_ids"] for row in no_answer), 6) if no_answer else 0.0,
        "no_answer_cases": len(no_answer),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 3),
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "p99": round(percentile(latencies, 0.99), 3),
            "max": round(max(latencies, default=0.0), 3),
        },
    }


def pct(value: float) -> str:
    return f"{value:.2%}"


def report_text(metrics: dict[str, Any], failures: list[dict[str, Any]], seed: dict[str, Any]) -> str:
    retrieval = metrics["current_retrieval"]
    latency = metrics["latency_ms"]
    lines = [
        "# Ask P1-Memory 工具执行与证据召回报告",
        "",
        f"- 数据：Layer3 冻结池 {metrics['total_cases']} 条；单一 Memory 类型可独立执行 {metrics['single_tool_cases']} 条。",
        f"- 隔离：临时 PostgreSQL space；去重 Memory {seed['unique_memories']} 条；成功结束后删除。",
        "- 路径：直接调用公开工具 `search_memories/search_tasks`；无回答 LLM、无 Cross-Encoder。",
        "",
        "## 总体",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 工具执行成功率 | {pct(metrics['execution_success_rate'])} |",
        f"| Recall@1/3/5/10 | {pct(retrieval['recall_at_1'])} / {pct(retrieval['recall_at_3'])} / {pct(retrieval['recall_at_5'])} / {pct(retrieval['recall_at_10'])} |",
        f"| MRR / MAP@10 / NDCG@10 | {retrieval['mrr']:.4f} / {retrieval['ap_at_10']:.4f} / {retrieval['ndcg_at_10']:.4f} |",
        f"| 状态 Top1 Accuracy | {pct(metrics['state_accuracy'])} ({metrics['state_cases']} cases) |",
        f"| History Recall@10 | {pct(metrics['history_recall_at_10'])} ({metrics['history_cases']} cases) |",
        f"| Must-not@10 违规率 | {pct(metrics['must_not_violation_rate_at_10'])} |",
        f"| No-answer 空结果率 | {pct(metrics['no_answer_empty_rate'])} ({metrics['no_answer_cases']} cases) |",
        f"| 延迟 Mean/P50/P95/P99 | {latency['mean']:.1f}/{latency['p50']:.1f}/{latency['p95']:.1f}/{latency['p99']:.1f} ms |",
        "",
        "## 分类型",
        "",
        "| 类型 | Cases | R@1 | R@3 | R@5 | R@10 | MRR | NDCG@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in metrics["by_memory_type"].items():
        lines.append(
            f"| {name} | {row['cases']} | {pct(row['recall_at_1'])} | {pct(row['recall_at_3'])} | {pct(row['recall_at_5'])} | {pct(row['recall_at_10'])} | {row['mrr']:.4f} | {row['ndcg_at_10']:.4f} |"
        )
    lines += [
        "",
        "## 结论边界",
        "",
        f"- 严格失败样本 {len(failures)} 条，见 `failures.jsonl`。",
        "- 多类型、多问题联合调用不计入本阶段，留到 P2。",
        "- 本阶段不评价最终回答与引用。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    cases, contract = load_cases_with_manifest(args.data_dir)
    pool = FastSharedPool(cases, args.run_id)
    original_vector_lifecycle = postgres_memory.MEMORY_VECTOR_LIFECYCLE_ENABLED
    postgres_memory.MEMORY_VECTOR_LIFECYCLE_ENABLED = False
    try:
        # SharedPool materializes every vector in one batch immediately after
        # seeding, so per-row pending-vector/outbox scheduling is redundant.
        seed = pool.seed()
    finally:
        postgres_memory.MEMORY_VECTOR_LIFECYCLE_ENABLED = original_vector_lifecycle
    rows = []
    succeeded = False
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
            futures = [executor.submit(evaluate_case, pool, case) for case in cases]
            for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
                row = future.result()
                rows.append(row)
                if index % 20 == 0 or index == len(futures):
                    print(f"progress={index}/{len(futures)}", flush=True)
        rows.sort(key=lambda row: row["case_id"])
        metrics = aggregate(rows)
        failures = [
            row
            for row in rows
            if row["current_error"]
            or row["history_error"]
            or row["must_not_violation"]
            or row["state_correct"] is False
            or (row["current_metrics"] and row["current_metrics"]["recall_at_5"] < 1.0)
            or (row["history_recall_at_10"] is not None and row["history_recall_at_10"] < 1.0)
        ]
        manifest = {
            "run_id": args.run_id,
            "case_count": len(cases),
            "contract": contract,
            "seed": seed,
            "public_tools": ["search_memories", "search_tasks"],
            "answer_llm_called": False,
            "cross_encoder": False,
            "isolated_space": True,
            "test_space_removed_on_success": True,
        }
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with (output / "cases.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        with (output / "failures.jsonl").open("w", encoding="utf-8") as handle:
            for row in failures:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        (output / "ASK_P1_MEMORY_TOOL_EVALUATION.md").write_text(report_text(metrics, failures, seed), encoding="utf-8")
        succeeded = True
        print(json.dumps({"metrics": metrics, "failure_count": len(failures)}, ensure_ascii=False, indent=2), flush=True)
    finally:
        if succeeded:
            pool.cleanup()


if __name__ == "__main__":
    main()
