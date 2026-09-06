"""P1 Ask evaluation: execute public search_notes on frozen LongMemEval-S unseen120."""

from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import pickle
import statistics
import sys
import time
from typing import Any

ROOT = Path.cwd()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import query_agent
from core.config import get_embedding_config
from eval.external.longmemeval import run_longmemeval_s_retrieval as lme
from infrastructure.database import session_scope
from infrastructure.schema import Note, NoteEmbedding, NoteTag, Space
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

TOP_KS = (1, 3, 5, 10)


def normalize(text: str) -> str:
    return " ".join(str(text or "").split())


def session_text(session: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{str(turn.get('role') or 'unknown').strip()}: {normalize(turn.get('content'))}"
        for turn in session
        if str(turn.get("content") or "").strip()
    )


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


def rank_metrics(ranked: list[str], expected: set[str]) -> dict[str, float]:
    result = {}
    for cutoff in TOP_KS:
        found = len(expected & set(ranked[:cutoff]))
        result[f"hit_at_{cutoff}"] = float(found > 0)
        result[f"recall_at_{cutoff}"] = found / len(expected) if expected else 0.0
        result[f"precision_at_{cutoff}"] = found / cutoff
    first = next((rank for rank, item in enumerate(ranked, 1) if item in expected), None)
    result["mrr"] = 1.0 / first if first else 0.0
    dcg = sum(1.0 / math.log2(rank + 1) for rank, item in enumerate(ranked[:10], 1) if item in expected)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(expected), 10) + 1))
    result["ndcg_at_10"] = dcg / ideal if ideal else 0.0
    return result


def mean_metrics(rows: list[dict[str, float]]) -> dict[str, float]:
    return {key: round(statistics.fmean(row[key] for row in rows), 6) for key in rows[0]} if rows else {}


def seed_case(
    case: dict[str, Any],
    space_id: str,
    embeddings: dict[str, list[float]],
) -> tuple[dict[str, list[str]], int, dict[str, int]]:
    sessions = case.get("haystack_sessions") or []
    session_ids = case.get("haystack_session_ids") or []
    dates = case.get("haystack_dates") or []
    if not (len(sessions) == len(session_ids) == len(dates)):
        raise ValueError("invalid LongMemEval-S session arrays")
    mapping: dict[str, list[str]] = {}
    note_rows = []
    tag_rows = []
    vector_rows = []
    vector_ready = 0
    vector_missing = 0
    for position, (session, session_id, date) in enumerate(zip(sessions, session_ids, dates, strict=True)):
        note_id = lme._note_id(str(case["question_id"]), str(session_id), position, namespace=space_id)
        message_id = f"ask-p1-note-{lme._safe_id(str(case['question_id']))}-{position}"
        text = session_text(session)
        source_time = lme._to_iso(str(date))
        tags = ["benchmark", "longmemeval-s", str(case.get("question_type") or "")]
        mapping.setdefault(str(session_id), []).append(note_id)
        note_rows.append(
            {
                "id": note_id,
                "message_id": message_id,
                "tenant_id": DEFAULT_TENANT_ID,
                "space_id": space_id,
                "created_at": parse_datetime(source_time),
                "title": f"LongMemEval-S session {session_id}",
                "note_type": "benchmark_session",
                "summary": text[:600],
                "text": text,
                "metadata_json": {},
                "enrichment_status": "ready",
                "enrichment_attempts": 0,
                "sensitivity": "normal",
            }
        )
        tag_rows.extend({"note_id": note_id, "tag": tag} for tag in tags)
        embedding = embeddings.get(normalize(text))
        if embedding is None:
            vector_missing += 1
        else:
            vector_ready += 1
            vector_rows.append(
                {
                    "note_id": note_id,
                    "model": get_embedding_config().model,
                    "dimensions": len(embedding),
                    "embedding": embedding,
                    "text": text,
                    "metadata_json": {
                        "message_id": message_id,
                        "title": f"LongMemEval-S session {session_id}",
                        "tags": tags,
                        "type": "benchmark_session",
                        "summary": text[:600],
                        "ts": source_time,
                        "embedding_model": get_embedding_config().model,
                    },
                }
            )
    with session_scope() as session:
        internal_space = ensure_tenant_space(
            session, space_id, tenant_id=DEFAULT_TENANT_ID, source="ask_p1_note_eval", metadata={"isolated": True}
        )
        for row in note_rows:
            row["space_id"] = internal_space
        inserted = list(
            session.execute(
                insert(Note).values(note_rows).on_conflict_do_nothing(constraint="uq_notes_space_message").returning(Note.id)
            ).scalars()
        )
        if len(inserted) != len(note_rows):
            raise RuntimeError(f"note insert conflict: {len(inserted)}/{len(note_rows)}")
        if tag_rows:
            session.execute(insert(NoteTag).values(tag_rows).on_conflict_do_nothing())
        if vector_rows:
            session.execute(
                insert(NoteEmbedding)
                .values(vector_rows)
                .on_conflict_do_nothing(index_elements=[NoteEmbedding.note_id, NoteEmbedding.model])
            )
    return mapping, len(note_rows), {
        "vector_ready": vector_ready,
        "vector_missing": vector_missing,
    }


def cleanup_run_spaces(run_id: str) -> int:
    prefix = f"ask_p1_note_{run_id}_%"
    with session_scope() as session:
        result = session.execute(delete(Space).where(Space.id.like(prefix)))
        return int(result.rowcount or 0)


def _chunks(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[start : start + size] for start in range(0, len(rows), size)]


def seed_all_cases(
    cases: list[dict[str, Any]],
    run_id: str,
    embeddings: dict[str, list[float]],
) -> dict[str, dict[str, Any]]:
    """Seed all isolated spaces in bounded batches to avoid per-case SSH transactions."""
    config = get_embedding_config()
    fixtures: dict[str, dict[str, Any]] = {}
    space_rows: list[dict[str, Any]] = []
    note_rows: list[dict[str, Any]] = []
    tag_rows: list[dict[str, Any]] = []
    vector_rows: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["question_id"])
        space_id = f"ask_p1_note_{run_id}_{lme._safe_id(case_id)}"
        sessions = case.get("haystack_sessions") or []
        session_ids = case.get("haystack_session_ids") or []
        dates = case.get("haystack_dates") or []
        if not (len(sessions) == len(session_ids) == len(dates)):
            raise ValueError(f"invalid LongMemEval-S session arrays: {case_id}")
        mapping: dict[str, list[str]] = {}
        vector_ready = 0
        vector_missing = 0
        space_rows.append(
            {
                "id": space_id,
                "tenant_id": DEFAULT_TENANT_ID,
                "source": "ask_p1_note_eval",
                "source_space_id": space_id,
                "space_type": "chat",
                "metadata_json": {"isolated": True, "run_id": run_id},
                "processed_sequence_no": 0,
                "note_watermark": 0,
                "memory_watermark": 0,
            }
        )
        for position, (conversation, session_id, date) in enumerate(zip(sessions, session_ids, dates, strict=True)):
            note_id = lme._note_id(case_id, str(session_id), position, namespace=space_id)
            message_id = f"ask-p1-note-{lme._safe_id(case_id)}-{position}"
            text = session_text(conversation)
            source_time = lme._to_iso(str(date))
            tags = ["benchmark", "longmemeval-s", str(case.get("question_type") or "")]
            mapping.setdefault(str(session_id), []).append(note_id)
            note_rows.append(
                {
                    "id": note_id,
                    "message_id": message_id,
                    "tenant_id": DEFAULT_TENANT_ID,
                    "space_id": space_id,
                    "created_at": parse_datetime(source_time),
                    "title": f"LongMemEval-S session {session_id}",
                    "note_type": "benchmark_session",
                    "summary": text[:600],
                    "text": text,
                    "metadata_json": {},
                    "enrichment_status": "ready",
                    "enrichment_attempts": 0,
                    "sensitivity": "normal",
                }
            )
            tag_rows.extend({"note_id": note_id, "tag": tag} for tag in tags)
            embedding = embeddings.get(normalize(text))
            if embedding is None:
                vector_missing += 1
            else:
                vector_ready += 1
                vector_rows.append(
                    {
                        "note_id": note_id,
                        "model": config.model,
                        "dimensions": len(embedding),
                        "embedding": embedding,
                        "text": text,
                        "metadata_json": {
                            "message_id": message_id,
                            "title": f"LongMemEval-S session {session_id}",
                            "tags": tags,
                            "type": "benchmark_session",
                            "summary": text[:600],
                            "ts": source_time,
                            "embedding_model": config.model,
                        },
                    }
                )
        fixtures[case_id] = {
            "space_id": space_id,
            "mapping": mapping,
            "note_count": len(sessions),
            "vector_ready": vector_ready,
            "vector_missing": vector_missing,
        }
    with session_scope() as session:
        session.execute(insert(Space).values(space_rows).on_conflict_do_nothing(index_elements=[Space.id]))
        for batch in _chunks(note_rows, 50):
            session.execute(insert(Note).values(batch).on_conflict_do_nothing(constraint="uq_notes_space_message"))
        for batch in _chunks(tag_rows, 750):
            session.execute(insert(NoteTag).values(batch).on_conflict_do_nothing())
        for batch in _chunks(vector_rows, 100):
            session.execute(
                insert(NoteEmbedding)
                .values(batch)
                .on_conflict_do_nothing(index_elements=[NoteEmbedding.note_id, NoteEmbedding.model])
            )
    return fixtures


def evaluate_seeded_case(case: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any]:
    started_total = time.perf_counter()
    error = None
    rows: list[dict[str, Any]] = []
    retrieval_ms = 0.0
    try:
        started = time.perf_counter()
        rows = query_agent.search_notes(fixture["space_id"], str(case.get("question") or ""), limit=10, min_score=0.0)
        retrieval_ms = (time.perf_counter() - started) * 1000
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    expected = {
        note_id
        for session_id in case.get("answer_session_ids") or []
        for note_id in fixture["mapping"].get(str(session_id), [])
    }
    ranked = [str(row.get("id") or "") for row in rows if row.get("id")]
    return {
        "case_id": str(case["question_id"]),
        "question_type": case.get("question_type"),
        "question": case.get("question"),
        "note_count": fixture["note_count"],
        "vector_ready": fixture["vector_ready"],
        "vector_missing": fixture["vector_missing"],
        "expected_note_ids": sorted(expected),
        "ranked_note_ids": ranked,
        "retrieval_channels": {str(row.get("id")): row.get("retrieval_channels") or [] for row in rows},
        "error": error,
        "execution_ok": error is None,
        "metrics": rank_metrics(ranked, expected) if expected else None,
        "retrieval_latency_ms": round(retrieval_ms, 3),
        "total_case_latency_ms": round((time.perf_counter() - started_total) * 1000, 3),
    }


def evaluate_case(case: dict[str, Any], run_id: str, embeddings: dict[str, list[float]]) -> dict[str, Any]:
    case_id = str(case["question_id"])
    space_id = f"ask_p1_note_{run_id}_{lme._safe_id(case_id)}"
    started_total = time.perf_counter()
    error = None
    rows: list[dict[str, Any]] = []
    mapping: dict[str, list[str]] = {}
    note_count = 0
    vector_seed = {"vector_ready": 0, "vector_missing": 0}
    retrieval_ms = 0.0
    try:
        mapping, note_count, vector_seed = seed_case(case, space_id, embeddings)
        started = time.perf_counter()
        rows = query_agent.search_notes(space_id, str(case.get("question") or ""), limit=10, min_score=0.0)
        retrieval_ms = (time.perf_counter() - started) * 1000
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    expected = {note_id for session_id in case.get("answer_session_ids") or [] for note_id in mapping.get(str(session_id), [])}
    ranked = [str(row.get("id") or "") for row in rows if row.get("id")]
    return {
        "case_id": case_id,
        "question_type": case.get("question_type"),
        "question": case.get("question"),
        "note_count": note_count,
        "vector_ready": vector_seed["vector_ready"],
        "vector_missing": vector_seed["vector_missing"],
        "expected_note_ids": sorted(expected),
        "ranked_note_ids": ranked,
        "retrieval_channels": {str(row.get("id")): row.get("retrieval_channels") or [] for row in rows},
        "error": error,
        "execution_ok": error is None,
        "metrics": rank_metrics(ranked, expected) if expected else None,
        "retrieval_latency_ms": round(retrieval_ms, 3),
        "total_case_latency_ms": round((time.perf_counter() - started_total) * 1000, 3),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row["metrics"] is not None]
    latencies = [float(row["retrieval_latency_ms"]) for row in rows if row["execution_ok"]]
    by_type = {}
    for name in sorted({str(row["question_type"]) for row in valid}):
        bucket = [row["metrics"] for row in valid if str(row["question_type"]) == name]
        by_type[name] = {"cases": len(bucket), **mean_metrics(bucket)}
    note_count = sum(int(row["note_count"]) for row in rows)
    vector_ready = sum(int(row["vector_ready"]) for row in rows)
    return {
        "cases": len(rows),
        "execution_success_rate": round(sum(row["execution_ok"] for row in rows) / len(rows), 6),
        "retrieval": mean_metrics([row["metrics"] for row in valid]),
        "seeded_note_count": note_count,
        "vector_ready": vector_ready,
        "vector_missing": sum(int(row["vector_missing"]) for row in rows),
        "vector_coverage": round(vector_ready / note_count, 6) if note_count else 0.0,
        "by_question_type": by_type,
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "p99": round(percentile(latencies, 0.99), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
    }


def pct(value: float) -> str:
    return f"{value:.2%}"


def report_text(metrics: dict[str, Any], failures: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
    r = metrics["retrieval"]
    latency = metrics["latency_ms"]
    lines = [
        "# Ask P1-Note 工具执行与证据召回报告",
        "",
        f"- 数据：LongMemEval-S unseen {metrics['cases']} 条；共检索 {manifest['seeded_note_count']} 个会话 Note。",
        "- 路径：直接调用公开工具 `search_notes`；Redis embedding 快照，只读缓存，外部 embedding 请求 0。",
        f"- 向量覆盖：{manifest['vector_ready']}/{manifest['seeded_note_count']} ({pct(manifest['vector_coverage'])})；缺失向量的 Note 仍参加 Exact/FTS/Lexical。",
        "- 隔离：每 Case 独立 PostgreSQL space，执行后删除。",
        "",
        "## 总体",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 工具执行成功率 | {pct(metrics['execution_success_rate'])} |",
        f"| Hit@1/3/5/10 | {pct(r['hit_at_1'])} / {pct(r['hit_at_3'])} / {pct(r['hit_at_5'])} / {pct(r['hit_at_10'])} |",
        f"| Evidence Recall@1/3/5/10 | {pct(r['recall_at_1'])} / {pct(r['recall_at_3'])} / {pct(r['recall_at_5'])} / {pct(r['recall_at_10'])} |",
        f"| MRR / NDCG@10 | {r['mrr']:.4f} / {r['ndcg_at_10']:.4f} |",
        f"| 检索延迟 Mean/P50/P95/P99 | {latency['mean']:.1f}/{latency['p50']:.1f}/{latency['p95']:.1f}/{latency['p99']:.1f} ms |",
        "",
        "## 分题型",
        "",
        "| 类型 | Cases | H@1 | H@3 | H@5 | H@10 | MRR | NDCG@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in metrics["by_question_type"].items():
        lines.append(
            f"| {name} | {row['cases']} | {pct(row['hit_at_1'])} | {pct(row['hit_at_3'])} | {pct(row['hit_at_5'])} | {pct(row['hit_at_10'])} | {row['mrr']:.4f} | {row['ndcg_at_10']:.4f} |"
        )
    lines += [
        "",
        "## 结论边界",
        "",
        f"- 严格失败样本 {len(failures)} 条，见 `failures.jsonl`。",
        "- 本阶段只测单次 `search_notes`，不含 Query Rewrite、多工具补召回和最终回答。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    cases = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    snapshot = pickle.loads(Path(args.snapshot).read_bytes())
    config = get_embedding_config()
    if snapshot.get("model") != config.model or int(snapshot.get("dimension") or 0) != config.dimension:
        raise RuntimeError("embedding snapshot does not match current config")
    embeddings = {normalize(key): value for key, value in (snapshot.get("embeddings") or {}).items()}
    original_embed = query_agent.embed_text
    query_agent.embed_text = lambda text: embeddings[normalize(text)]
    rows = []
    cleaned_spaces = 0
    try:
        fixtures = seed_all_cases(cases, args.run_id, embeddings)
        print(f"seeded={len(fixtures)}", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
            futures = [
                executor.submit(evaluate_seeded_case, case, fixtures[str(case["question_id"])]) for case in cases
            ]
            for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
                rows.append(future.result())
                if index % 20 == 0 or index == len(futures):
                    print(f"progress={index}/{len(futures)}", flush=True)
    finally:
        query_agent.embed_text = original_embed
        cleaned_spaces = cleanup_run_spaces(args.run_id)
    rows.sort(key=lambda row: row["case_id"])
    metrics = aggregate(rows)
    failures = [row for row in rows if not row["execution_ok"] or (row["metrics"] and row["metrics"]["hit_at_5"] < 1.0)]
    manifest = {
        "run_id": args.run_id,
        "dataset": args.dataset,
        "case_count": len(rows),
        "seeded_note_count": metrics["seeded_note_count"],
        "vector_ready": metrics["vector_ready"],
        "vector_missing": metrics["vector_missing"],
        "vector_coverage": metrics["vector_coverage"],
        "public_tool": "search_notes",
        "embedding_snapshot": args.snapshot,
        "external_embedding_requests": 0,
        "answer_llm_called": False,
        "cross_encoder": False,
        "isolated_spaces": True,
        "cleaned_spaces": cleaned_spaces,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "failures.jsonl").open("w", encoding="utf-8") as handle:
        for row in failures:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (output / "ASK_P1_NOTE_TOOL_EVALUATION.md").write_text(report_text(metrics, failures, manifest), encoding="utf-8")
    print(json.dumps({"metrics": metrics, "failure_count": len(failures)}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
