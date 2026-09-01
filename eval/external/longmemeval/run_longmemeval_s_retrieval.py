"""Isolated LongMemEval-S session-retrieval evaluation for Suixinji.

This runner measures the real Note hybrid retrieval path under distractor
history. It deliberately does not call an answer LLM and it does not create
Memory objects, so results are retrieval metrics rather than end-to-end QA.
Each case receives a disposable PostgreSQL space deleted in ``finally``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete

from core.config import get_embedding_config
from core.llm_client import embed_text
from infrastructure.database import session_scope
from infrastructure.schema import Space
from repositories.postgres.notes import hybrid_search_notes, save_note
from repositories.postgres.vectors import add_vector_item
from storage.note_storage import NoteMetadata
from storage.vector_store import VectorItem


ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = ROOT / "longmemeval_s_cleaned.json"
RESULTS_DIR = ROOT / "results"
TOP_KS = (1, 3, 5, 10)


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", value)[:96]


def _case_space_id(case_id: str) -> str:
    return f"eval-longmemeval-s-{hashlib.sha256(case_id.encode('utf-8')).hexdigest()[:16]}"


def _note_id(case_id: str, session_id: str, position: int, *, namespace: str | None = None) -> str:
    # LongMemEval may repeat a session label inside one case.  Note.id is a
    # global database primary key, so the fixture position is part of the
    # physical identity.  Include the disposable space namespace so a crashed
    # run cannot collide with a later isolated run of the same benchmark case.
    identity = f"{case_id}:{session_id}:{position}"
    if namespace:
        identity = f"{namespace}:{identity}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"lmes-note-{digest}"


def _to_iso(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y/%m/%d (%a) %H:%M").replace(tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def _session_text(session: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{str(turn.get('role') or 'unknown').strip()}: {' '.join(str(turn.get('content') or '').split())}"
        for turn in session
        if str(turn.get("content") or "").strip()
    )


def _write_case_notes(
    case: dict[str, Any],
    space_id: str,
    *,
    with_embeddings: bool,
    embedding_workers: int,
) -> tuple[dict[str, list[str]], int, dict[str, int]]:
    sessions = case.get("haystack_sessions") or []
    session_ids = case.get("haystack_session_ids") or []
    dates = case.get("haystack_dates") or []
    if not (len(sessions) == len(session_ids) == len(dates)):
        raise ValueError(f"invalid LongMemEval-S session arrays: {case.get('question_id')}")
    mapping: dict[str, list[str]] = {}
    pending_embeddings: list[tuple[str, str, str, dict[str, Any]]] = []
    for position, (session, session_id, date) in enumerate(zip(sessions, session_ids, dates, strict=True)):
        note_id = _note_id(str(case["question_id"]), str(session_id), position, namespace=space_id)
        text = _session_text(session)
        saved = save_note(NoteMetadata(
            id=note_id,
            message_id=f"lmes-message-{_safe_id(str(case['question_id']))}-{position}",
            space_id=space_id,
            ts=_to_iso(str(date)),
            title=f"LongMemEval-S session {session_id}",
            tags=["benchmark", "longmemeval-s", str(case.get("question_type") or "")],
            type="benchmark_session",
            summary=text[:600], text=text, related=[], enrichment_status="ready",
        ))
        if not saved:
            raise RuntimeError(f"note insert conflict: {note_id}")
        mapping.setdefault(str(session_id), []).append(note_id)
        pending_embeddings.append((note_id, f"lmes-message-{_safe_id(str(case['question_id']))}-{position}", text, {
            "title": f"LongMemEval-S session {session_id}",
            "tags": ["benchmark", "longmemeval-s", str(case.get("question_type") or "")],
            "type": "benchmark_session", "summary": text[:600], "ts": _to_iso(str(date)),
            "embedding_model": str(get_embedding_config().model),
        }))
    stats = {"note_embedding_ready": 0, "note_embedding_failed": 0}
    if not with_embeddings:
        return mapping, len(pending_embeddings), stats

    def write_embedding(item: tuple[str, str, str, dict[str, Any]]) -> bool:
        note_id, message_id, text, metadata = item
        return add_vector_item(space_id, VectorItem(
            note_id=note_id,
            message_id=message_id,
            text=text,
            embedding=embed_text(text),
            metadata=metadata,
        ))

    with ThreadPoolExecutor(max_workers=max(1, min(int(embedding_workers), 8))) as pool:
        futures = [pool.submit(write_embedding, item) for item in pending_embeddings]
        for future in as_completed(futures):
            try:
                if future.result():
                    stats["note_embedding_ready"] += 1
                else:
                    stats["note_embedding_failed"] += 1
            except Exception:
                stats["note_embedding_failed"] += 1
    return mapping, len(pending_embeddings), stats


def _cleanup_space(space_id: str) -> None:
    with session_scope() as session:
        session.execute(delete(Space).where(Space.id == space_id))


def _stratified_cases(cases: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or limit >= len(cases):
        return cases
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        buckets[str(case.get("question_type") or "unknown")].append(case)
    selected: list[dict[str, Any]] = []
    cursors = {name: 0 for name in buckets}
    while len(selected) < limit:
        progressed = False
        for name in sorted(buckets):
            if len(selected) >= limit:
                break
            if cursors[name] < len(buckets[name]):
                selected.append(buckets[name][cursors[name]])
                cursors[name] += 1
                progressed = True
        if not progressed:
            break
    return selected


def _recall(retrieved: list[str], expected: set[str], k: int) -> bool | None:
    return bool(expected.intersection(retrieved[:k])) if expected else None


def _coverage(retrieved: list[str], expected: set[str], k: int) -> float | None:
    return round(len(set(retrieved[:k]).intersection(expected)) / len(expected), 4) if expected else None


def _run_case(
    case: dict[str, Any],
    *,
    keep_space: bool,
    with_embeddings: bool,
    embedding_workers: int,
) -> dict[str, Any]:
    case_id = str(case["question_id"])
    space_id = _case_space_id(case_id)
    _cleanup_space(space_id)
    started = time.perf_counter()
    try:
        mapping, history_sessions, embedding_stats = _write_case_notes(
            case,
            space_id,
            with_embeddings=with_embeddings,
            embedding_workers=embedding_workers,
        )
        query_started = time.perf_counter()
        query_embedding = embed_text(str(case["question"])) if with_embeddings else None
        records = hybrid_search_notes(
            space_id,
            str(case["question"]),
            query_embedding=query_embedding,
            limit=max(TOP_KS),
        )
        retrieval_seconds = time.perf_counter() - query_started
        retrieved = [str(record.get("id") or record.get("note_id") or "") for record in records]
        expected = {
            note_id
            for session_id in case.get("answer_session_ids") or []
            for note_id in mapping.get(str(session_id), [])
        }
        row: dict[str, Any] = {
            "question_id": case_id, "question_type": case.get("question_type"), "question": case.get("question"),
            "history_sessions": history_sessions, "expected_note_ids": sorted(expected), "retrieved_note_ids": retrieved,
            "is_abstention": not expected, "retrieval_seconds": round(retrieval_seconds, 4),
            "embedding_mode": "real" if with_embeddings else "sparse_only",
            **embedding_stats,
            "total_case_seconds": round(time.perf_counter() - started, 4), "error": None,
        }
        for k in TOP_KS:
            row[f"any_answer_session_recall_at_{k}"] = _recall(retrieved, expected, k)
            row[f"answer_session_coverage_at_{k}"] = _coverage(retrieved, expected, k)
        return row
    except Exception as exc:
        return {
            "question_id": case_id, "question_type": case.get("question_type"), "question": case.get("question"),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            "total_case_seconds": round(time.perf_counter() - started, 4),
        }
    finally:
        if not keep_space:
            _cleanup_space(space_id)


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None and not row.get("error")]
    return round(statistics.mean(values), 4) if values else None


def _rate(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [bool(row[key]) for row in rows if row.get(key) is not None and not row.get("error")]
    return round(sum(values) / len(values), 4) if values else None


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))], 4)


def _summary(rows: list[dict[str, Any]], dataset: Path, *, embedding_mode: str) -> dict[str, Any]:
    valid = [row for row in rows if not row.get("error")]
    latencies = [float(row["retrieval_seconds"]) for row in valid]
    output: dict[str, Any] = {
        "benchmark": "LongMemEval-S cleaned", "dataset": str(dataset),
        "evaluation_scope": "isolated PostgreSQL spaces + real Suixinji Note hybrid retrieval; no LLM answer, no Memory extraction",
        "embedding_mode": embedding_mode,
        "cases": len(rows), "completed": len(valid), "failed": len(rows) - len(valid),
        "abstention_cases": sum(bool(row.get("is_abstention")) for row in valid),
        "history_sessions_mean": _mean(valid, "history_sessions"),
        "retrieval_latency_seconds": {
            "mean": round(statistics.mean(latencies), 4) if latencies else None,
            "p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95),
        },
        "limitations": [
            "This is session-level retrieval only; it does not report answer accuracy or LongMemEval's official GPT-4o judge.",
            "No Memory extraction is performed; typed Memory retrieval is assessed separately by the full-lifecycle evaluation.",
            "Abstention cases have no answer-evidence session and are excluded from recall/coverage denominators.",
        ],
    }
    for k in TOP_KS:
        output[f"any_answer_session_recall_at_{k}"] = _rate(valid, f"any_answer_session_recall_at_{k}")
        output[f"answer_session_coverage_at_{k}"] = _mean(valid, f"answer_session_coverage_at_{k}")
    by_type: dict[str, Any] = {}
    for name in sorted({str(row.get("question_type") or "unknown") for row in rows}):
        bucket = [row for row in valid if str(row.get("question_type") or "unknown") == name]
        by_type[name] = {"cases": len(bucket), **{f"any_answer_session_recall_at_{k}": _rate(bucket, f"any_answer_session_recall_at_{k}") for k in TOP_KS}}
    output["by_question_type"] = by_type
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int, default=60, help="0 means all 500 cases")
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--keep-spaces", action="store_true", help="debug only; never use for a normal run")
    parser.add_argument("--with-embeddings", action="store_true", help="write real NoteEmbedding rows before retrieval")
    parser.add_argument("--embedding-workers", type=int, default=4, help="bounded embedding concurrency (1-8)")
    args = parser.parse_args()
    selected = _stratified_cases(json.loads(args.dataset.read_text(encoding="utf-8")), args.limit)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows_path = args.output_dir / f"longmemeval_s_note_hybrid_{stamp}.jsonl"
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(selected, start=1):
        row = _run_case(
            case,
            keep_space=args.keep_spaces,
            with_embeddings=args.with_embeddings,
            embedding_workers=args.embedding_workers,
        )
        rows.append(row)
        with rows_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps({"progress": f"{index}/{len(selected)}", "question_id": row["question_id"], "error": row.get("error")}, ensure_ascii=False), flush=True)
    summary_path = rows_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(
        _summary(rows, args.dataset, embedding_mode="real" if args.with_embeddings else "sparse_only"),
        ensure_ascii=False,
        indent=2,
    ) + "\n", encoding="utf-8")
    print(json.dumps({"rows": str(rows_path), "summary": str(summary_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
