"""LongMemEval-S retrieval V2: held-out evaluation with safe ranking upgrades.

This runner is intentionally evaluation-only.  It keeps the production Note
hybrid retriever as its candidate generator, then measures optional query
rewrite, role-aware scoring and a local CrossEncoder only on a held-out split.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.llm_client import embed_text
from agent.note_reranker import fuse_note_variants, rerank_note_records
from repositories.postgres.notes import hybrid_search_notes
from eval.external.longmemeval.run_longmemeval_s_retrieval import (
    DEFAULT_DATASET,
    RESULTS_DIR,
    TOP_KS,
    _cleanup_space,
    _note_id,
    _stratified_cases,
    _write_case_notes,
)

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "do", "did", "does", "i", "me", "my",
    "you", "your", "we", "our", "to", "of", "in", "on", "at", "for", "with", "and", "or",
    "what", "which", "who", "when", "where", "how", "can", "could", "would", "please", "about",
    "that", "this", "it", "all", "last", "time", "previously", "before", "after",
})
TEMPORAL_CUES = re.compile(r"\b(when|before|after|first|last|earlier|later|previous|timeline|how long|how many days|which .* first)\b", re.I)
MULTI_CUES = re.compile(r"\b(total|both|between|all|each|and|how many|how much|compare)\b", re.I)
ASSISTANT_CUES = re.compile(r"\b(you recommended|you suggested|you told me|you mentioned|we discussed|we talked|your recommendation)\b", re.I)
USER_CUES = re.compile(r"\b(i |my |did i|have i|when did i|what did i|where did i)\b", re.I)


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", value)[:72]


def _space_id(run_id: str, case_id: str) -> str:
    digest = hashlib.sha256(f"v2:{run_id}:{case_id}".encode("utf-8")).hexdigest()[:20]
    return f"eval-lmes-v2-{digest}"


def _tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", text.casefold()) if token not in STOPWORDS]


def _complex_kind(query: str) -> str | None:
    temporal = bool(TEMPORAL_CUES.search(query))
    multi = bool(MULTI_CUES.search(query))
    if temporal and multi:
        return "temporal_multi"
    if temporal:
        return "temporal"
    if multi:
        return "multi"
    return None


def _rewrite_queries(query: str) -> tuple[list[str], str | None]:
    """Deterministic rewrite; no labels, Gold fields, or answer text are read."""
    query = " ".join(str(query or "").split())
    terms = _tokens(query)
    kind = _complex_kind(query)
    variants = [query]
    if len(terms) >= 3:
        variants.append(" ".join(terms[:18]))
    # A complex question often contains multiple independently searchable
    # entities.  Preserve quoted/named clauses as extra retrieval lanes.
    if kind:
        quoted = re.findall(r"['\"]([^'\"]{3,120})['\"]", query)
        variants.extend(quoted[:2])
        pieces = re.split(r"\b(?:and|or|versus|vs\.?|between)\b", query, flags=re.I)
        variants.extend(" ".join(piece.split()) for piece in pieces if len(_tokens(piece)) >= 3)
    return list(dict.fromkeys(value for value in variants if value.strip()))[:4], kind


def _session_roles(case: dict[str, Any], *, namespace: str | None = None) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for position, (session, session_id) in enumerate(zip(
        case.get("haystack_sessions") or [], case.get("haystack_session_ids") or [], strict=True,
    )):
        roles = sorted({str(turn.get("role") or "unknown").casefold() for turn in session})
        output[_note_id(str(case["question_id"]), str(session_id), position, namespace=namespace)] = roles
    return output


def _query_role_mode(query: str) -> str | None:
    if ASSISTANT_CUES.search(query):
        return "assistant"
    if USER_CUES.search(query):
        return "user"
    return None


def _rrf_merge(groups: list[list[dict[str, Any]]], *, limit: int) -> list[dict[str, Any]]:
    return fuse_note_variants(groups, limit=limit)


class _CrossEncoderReranker:
    def __init__(self, model_name: str, proxy: str | None = None) -> None:
        self.model_name = model_name
        self.proxy = proxy
        self._model: Any = None
        self.status = "not_loaded"

    def _load(self) -> bool:
        if self._model is not None:
            return True
        try:
            if self.proxy:
                os.environ["HTTP_PROXY"] = self.proxy
                os.environ["HTTPS_PROXY"] = self.proxy
                os.environ["http_proxy"] = self.proxy
                os.environ["https_proxy"] = self.proxy
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
            import numpy as np
            import numpy.core as np_core
            np._core = np_core
            np._core.multiarray = np_core.multiarray
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(
                self.model_name,
                max_length=512,
                model_kwargs={"local_files_only": not bool(self.proxy)},
            )
            self.status = "ready"
            return True
        except Exception as exc:
            self.status = f"unavailable:{type(exc).__name__}"
            return False

    def score(self, query: str, records: list[dict[str, Any]], roles: dict[str, list[str]]) -> dict[str, float]:
        if not records or not self._load():
            return {}
        pairs: list[tuple[str, str]] = []
        ids: list[str] = []
        for record in records:
            note_id = str(record.get("id") or record.get("note_id") or "")
            role_text = ", ".join(roles.get(note_id) or ["unknown"])
            text = str(record.get("text") or record.get("summary") or "")[:3600]
            pairs.append((query, f"Conversation roles: {role_text}\n{text}"))
            ids.append(note_id)
        try:
            values = self._model.predict(pairs, batch_size=16, show_progress_bar=False)
            return {note_id: float(value) for note_id, value in zip(ids, values, strict=True)}
        except Exception as exc:
            self.status = f"failed:{type(exc).__name__}"
            return {}


def _heuristic_scores(query: str, records: list[dict[str, Any]], roles: dict[str, list[str]]) -> dict[str, float]:
    """Offline fallback reranker; no Gold labels or network dependency."""
    q_terms = set(_tokens(query))
    q_text = query.casefold()
    mode = _query_role_mode(query)
    scores: dict[str, float] = {}
    for record in records:
        note_id = str(record.get("id") or record.get("note_id") or "")
        text = " ".join(str(record.get(key) or "") for key in ("title", "summary", "text")).casefold()
        d_terms = set(_tokens(text))
        overlap = len(q_terms & d_terms) / max(1, len(q_terms))
        phrase = sum(1 for term in q_terms if term in text)
        role = 0.08 if mode and mode in set(roles.get(note_id) or []) else 0.0
        scores[note_id] = 0.72 * overlap + 0.02 * min(phrase, 8) + role
        if q_text and q_text in text:
            scores[note_id] += 0.25
    return scores


def _normalize(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if math.isclose(lo, hi):
        return {key: 1.0 for key in values}
    return {key: (value - lo) / (hi - lo) for key, value in values.items()}


def _rerank(
    query: str,
    records: list[dict[str, Any]],
    *,
    roles: dict[str, list[str]],
    reranker: _CrossEncoderReranker | None,
    alpha: float,
) -> list[dict[str, Any]]:
    if not records or reranker is None:
        return records
    cross = reranker.score(query, records, roles)
    if not cross:
        cross = _heuristic_scores(query, records, roles)
        if reranker.status.startswith("unavailable"):
            reranker.status = reranker.status + "+heuristic_fallback"
    if not cross:
        return records
    base = _normalize({str(item.get("id") or item.get("note_id")): float(item.get("v2_base_score") or 0.0) for item in records})
    cross = _normalize(cross)
    role_mode = _query_role_mode(query)
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in records:
        note_id = str(item.get("id") or item.get("note_id") or "")
        role_bonus = 0.025 if role_mode and role_mode in set(roles.get(note_id) or []) else 0.0
        score = alpha * cross.get(note_id, 0.0) + (1.0 - alpha) * base.get(note_id, 0.0) + role_bonus
        enriched = dict(item)
        enriched["v2_cross_score"] = round(cross.get(note_id, 0.0), 6)
        enriched["v2_role_bonus"] = role_bonus
        enriched["v2_final_score"] = round(score, 6)
        scored.append((score, enriched))
    return [item for _score, item in sorted(scored, key=lambda row: (row[0], str(row[1].get("ts") or "")), reverse=True)]


def _recall(ids: list[str], expected: set[str], k: int) -> bool | None:
    return bool(expected.intersection(ids[:k])) if expected else None


def _coverage(ids: list[str], expected: set[str], k: int) -> float | None:
    return len(set(ids[:k]).intersection(expected)) / len(expected) if expected else None


def _mrr(ids: list[str], expected: set[str]) -> float | None:
    if not expected:
        return None
    for index, note_id in enumerate(ids, start=1):
        if note_id in expected:
            return 1.0 / index
    return 0.0


def _ndcg(ids: list[str], expected: set[str], k: int = 10) -> float | None:
    if not expected:
        return None
    dcg = sum(1.0 / math.log2(index + 1) for index, note_id in enumerate(ids[:k], start=1) if note_id in expected)
    ideal = sum(1.0 / math.log2(index + 1) for index in range(1, min(len(expected), k) + 1))
    return dcg / ideal if ideal else None


def _split_cases(cases: list[dict[str, Any]], split: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        buckets[str(case.get("question_type") or "unknown")].append(case)
    dev, test = [], []
    for name in sorted(buckets):
        ordered = sorted(buckets[name], key=lambda row: hashlib.sha256(str(row["question_id"]).encode()).hexdigest())
        for index, row in enumerate(ordered):
            (dev if index % 2 == 0 else test).append(row)
    manifest = {
        "version": "longmemeval-s-split-v1", "selection": "deterministic hash-stratified by question_type",
        "dev_question_ids": [str(row["question_id"]) for row in dev],
        "test_question_ids": [str(row["question_id"]) for row in test],
    }
    return ({"dev": dev, "test": test, "all": [*dev, *test]}[split], manifest)


def _run_case(
    case: dict[str, Any], *, run_id: str, with_embeddings: bool, embedding_workers: int,
    candidate_k: int, candidate_pool_k: int, apply_constraint_reranker: bool,
    reranker: _CrossEncoderReranker | None, alpha: float,
) -> dict[str, Any]:
    case_id = str(case["question_id"])
    space_id = _space_id(run_id, case_id)
    row: dict[str, Any]
    try:
        _cleanup_space(space_id)
        mapping, history_sessions, embedding_stats = _write_case_notes(
            case, space_id, with_embeddings=with_embeddings, embedding_workers=embedding_workers,
        )
        variants, complex_kind = _rewrite_queries(str(case["question"]))
        groups: list[list[dict[str, Any]]] = []
        retrieval_started = time.perf_counter()
        for variant in variants:
            embedding = embed_text(variant) if with_embeddings else None
            groups.append(hybrid_search_notes(space_id, variant, query_embedding=embedding, limit=candidate_k))
        candidate_records = _rrf_merge(groups, limit=candidate_pool_k)
        roles = _session_roles(case, namespace=space_id)
        if apply_constraint_reranker:
            candidate_records = rerank_note_records(str(case["question"]), candidate_records, roles_by_id=roles)
        ranked = _rerank(str(case["question"]), candidate_records, roles=roles, reranker=reranker, alpha=alpha)
        retrieval_seconds = time.perf_counter() - retrieval_started
        ids = [str(record.get("id") or record.get("note_id") or "") for record in ranked]
        expected = {
            note_id for session_id in case.get("answer_session_ids") or []
            for note_id in mapping.get(str(session_id), [])
        }
        row = {
            "question_id": case_id, "question_type": case.get("question_type"), "question": case.get("question"),
            "history_sessions": history_sessions, "expected_note_ids": sorted(expected), "retrieved_note_ids": ids,
            "is_abstention": not expected, "retrieval_seconds": round(retrieval_seconds, 4),
            "query_variants": variants, "complex_strategy": complex_kind, "query_role_mode": _query_role_mode(str(case["question"])),
            "reranker_status": reranker.status if reranker else "disabled", "embedding_mode": "real" if with_embeddings else "sparse_only",
            "candidate_k": candidate_k, "candidate_pool_k": candidate_pool_k,
            "constraint_reranker": apply_constraint_reranker, **embedding_stats, "error": None,
        }
        for k in TOP_KS:
            row[f"any_answer_session_recall_at_{k}"] = _recall(ids, expected, k)
            row[f"answer_session_coverage_at_{k}"] = _coverage(ids, expected, k)
        row["mrr"] = _mrr(ids, expected)
        row["ndcg_at_10"] = _ndcg(ids, expected)
    except Exception as exc:
        row = {"question_id": case_id, "question_type": case.get("question_type"), "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    finally:
        try:
            _cleanup_space(space_id)
        except Exception as cleanup_exc:
            cleanup_error = f"{type(cleanup_exc).__name__}: {str(cleanup_exc)[:300]}"
            if row.get("error"):
                row["error"] = f"{row['error']} | cleanup={cleanup_error}"
            else:
                row["error"] = f"cleanup={cleanup_error}"
    return row


def _load_checkpoint(path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    """Load a JSONL checkpoint; the latest valid row for each case wins."""
    rows: dict[str, dict[str, Any]] = {}
    invalid_lines = 0
    if not path.exists():
        return rows, invalid_lines
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
            case_id = str(row["question_id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            invalid_lines += 1
            continue
        rows[case_id] = row
    return rows, invalid_lines


def _append_checkpoint(path: Path, row: dict[str, Any]) -> None:
    """Durably append one completed case before the next case starts."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _run_with_retry(
    operation: Callable[[], dict[str, Any]],
    *,
    attempts: int,
    base_seconds: float,
    max_seconds: float,
) -> dict[str, Any]:
    """Retry one isolated case; only the final result participates in metrics."""
    result: dict[str, Any] = {"error": "case was not attempted"}
    bounded_attempts = max(1, attempts)
    for attempt in range(1, bounded_attempts + 1):
        result = operation()
        result["attempt_count"] = attempt
        result["recovered_after_retry"] = attempt > 1 and not result.get("error")
        if not result.get("error") or attempt >= bounded_attempts:
            return result
        delay = min(max(0.0, max_seconds), max(0.0, base_seconds) * (2 ** (attempt - 1)))
        print(json.dumps({"retry": attempt, "error": result.get("error"), "delay_seconds": delay}, ensure_ascii=False), flush=True)
        if delay:
            time.sleep(delay)
    return result


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None and not row.get("error")]
    return round(statistics.mean(values), 4) if values else None


def _rate(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [bool(row[key]) for row in rows if row.get(key) is not None and not row.get("error")]
    return round(sum(values) / len(values), 4) if values else None


def _summary(rows: list[dict[str, Any]], *, split: str, config: dict[str, Any]) -> dict[str, Any]:
    valid = [row for row in rows if not row.get("error")]
    out: dict[str, Any] = {
        "benchmark": "LongMemEval-S cleaned", "evaluation_scope": "held-out session retrieval; Note Hybrid candidate generation + deterministic rewrite/role signals + optional local CrossEncoder",
        "split": split, "config": config, "cases": len(rows), "completed": len(valid), "failed": len(rows) - len(valid),
        "latency_seconds": {"mean": _mean(valid, "retrieval_seconds"), "p95": sorted([row["retrieval_seconds"] for row in valid])[max(0, math.ceil(len(valid) * .95) - 1)] if valid else None},
        "limitations": ["Session retrieval only; not LongMemEval official answer/judge score.", "Split is for retrieval parameter selection; report only held-out test metrics as generalization.", "Role is derived from stored conversation turns, never from Gold evidence labels."],
    }
    for k in TOP_KS:
        out[f"recall_at_{k}"] = _rate(valid, f"any_answer_session_recall_at_{k}")
        out[f"coverage_at_{k}"] = _mean(valid, f"answer_session_coverage_at_{k}")
    out["mrr"] = _mean(valid, "mrr")
    out["ndcg_at_10"] = _mean(valid, "ndcg_at_10")
    by_type = {}
    for name in sorted({str(row.get("question_type") or "unknown") for row in valid}):
        bucket = [row for row in valid if str(row.get("question_type") or "unknown") == name]
        by_type[name] = {"cases": len(bucket), "recall_at_1": _rate(bucket, "any_answer_session_recall_at_1"), "recall_at_10": _rate(bucket, "any_answer_session_recall_at_10"), "mrr": _mean(bucket, "mrr")}
    out["by_question_type"] = by_type
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--split", choices=("dev", "test", "all"), default="test")
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR / "lme_s_v2")
    parser.add_argument("--run-id", default="lme-s-v2")
    parser.add_argument("--with-embeddings", action="store_true")
    parser.add_argument("--embedding-workers", type=int, default=4)
    parser.add_argument("--candidate-k", type=int, default=20, help="Per-query-variant Hybrid candidate depth.")
    parser.add_argument("--candidate-pool-k", type=int, default=0, help="Post-fusion pool depth; 0 keeps candidate-k.")
    parser.add_argument("--disable-constraint-reranker", action="store_true")
    parser.add_argument("--rerank-alpha", type=float, default=0.75)
    parser.add_argument("--disable-reranker", action="store_true")
    parser.add_argument("--cross-encoder-model", default=DEFAULT_MODEL)
    parser.add_argument("--hf-proxy", default=os.getenv("SUIXINJI_HF_PROXY", "http://127.0.0.1:7897"), help="HTTP proxy forwarded by SSH; empty means offline/cache only")
    parser.add_argument("--checkpoint-path", type=Path, help="Stable JSONL checkpoint used by --resume.")
    parser.add_argument("--resume", action="store_true", help="Skip successful cases already present in the checkpoint.")
    parser.add_argument("--case-attempts", type=int, default=4)
    parser.add_argument("--case-retry-base-seconds", type=float, default=2.0)
    parser.add_argument("--case-retry-max-seconds", type=float, default=15.0)
    args = parser.parse_args()
    selected = _stratified_cases(json.loads(args.dataset.read_text(encoding="utf-8")), args.limit)
    cases, manifest = _split_cases(selected, args.split)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "longmemeval_s_dev_test_split_v1.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reranker = None if args.disable_reranker else _CrossEncoderReranker(args.cross_encoder_model, proxy=args.hf_proxy or None)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows_path = args.checkpoint_path or args.output_dir / f"longmemeval_s_v2_{args.split}_{stamp}.jsonl"
    if rows_path.exists() and not args.resume:
        raise FileExistsError(f"checkpoint already exists: {rows_path}; use --resume or choose another path")
    latest_rows, invalid_checkpoint_lines = _load_checkpoint(rows_path) if args.resume else ({}, 0)
    successful_ids = {case_id for case_id, row in latest_rows.items() if not row.get("error")}
    pending_cases = [case for case in cases if str(case["question_id"]) not in successful_ids]
    print(json.dumps({
        "resume": args.resume,
        "checkpoint": str(rows_path),
        "total": len(cases),
        "completed_before_start": len(successful_ids),
        "pending": len(pending_cases),
        "invalid_checkpoint_lines": invalid_checkpoint_lines,
    }, ensure_ascii=False), flush=True)
    for case in pending_cases:
        per_variant_k = max(10, min(args.candidate_k, 30))
        candidate_pool_k = max(per_variant_k, min(args.candidate_pool_k or per_variant_k, 50))
        row = _run_with_retry(
            lambda: _run_case(
                case, run_id=args.run_id, with_embeddings=args.with_embeddings,
                embedding_workers=args.embedding_workers, candidate_k=per_variant_k,
                candidate_pool_k=candidate_pool_k,
                apply_constraint_reranker=not args.disable_constraint_reranker,
                reranker=reranker, alpha=min(.95, max(.05, args.rerank_alpha)),
            ),
            attempts=args.case_attempts,
            base_seconds=args.case_retry_base_seconds,
            max_seconds=args.case_retry_max_seconds,
        )
        _append_checkpoint(rows_path, row)
        latest_rows[str(case["question_id"])] = row
        if not row.get("error"):
            successful_ids.add(str(case["question_id"]))
        print(json.dumps({
            "progress": f"{len(successful_ids)}/{len(cases)}",
            "id": row["question_id"],
            "attempt_count": row.get("attempt_count"),
            "error": row.get("error"),
        }, ensure_ascii=False), flush=True)
    rows = [latest_rows[str(case["question_id"])] for case in cases if str(case["question_id"]) in latest_rows]
    summary = _summary(rows, split=args.split, config={
        "candidate_k": args.candidate_k, "candidate_pool_k": args.candidate_pool_k or args.candidate_k,
        "constraint_reranker": not args.disable_constraint_reranker,
        "rerank_alpha": args.rerank_alpha, "cross_encoder_model": args.cross_encoder_model,
        "hf_proxy": bool(args.hf_proxy), "reranker_status": reranker.status if reranker else "disabled",
        "rewrite": "deterministic_complex_only", "role_aware": True,
        "resume": args.resume, "checkpoint": str(rows_path),
        "case_attempts": args.case_attempts,
        "recovered_cases": sum(bool(row.get("recovered_after_retry")) for row in rows),
    })
    summary_path = rows_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rows": str(rows_path), "summary": str(summary_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
