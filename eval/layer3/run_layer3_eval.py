#!/usr/bin/env python3
"""Layer 3 retrieval/answer evaluation against the production Postgres query path.

The runner deliberately seeds only an isolated Postgres space and then calls
``memory_search`` and ``answer_question``.  Gold data is used by this file only
for scoring; it is never passed into application code.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# When this file is invoked by an absolute path, Python puts ``eval/layer3``
# (rather than the repository root) on sys.path.  Explicitly expose the
# production package root so the evaluator imports the same application code
# as the service process.
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DATA_FILES = (
    "current_state_retrieval.jsonl",
    "history_and_temporal.jsonl",
    "multi_memory_answer_and_citation.jsonl",
    "no_answer_conflict_and_stale.jsonl",
    "semantic_paraphrase_and_noise.jsonl",
)

NON_FACT_ANSWER_TYPES = {"no_answer", "conflict", "clarification", "restricted", "system_error"}


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def load_cases(data_dir: str) -> list[dict[str, Any]]:
    src = Path(data_dir)
    temp: Path | None = None
    if src.is_file() and src.suffix.lower() == ".zip":
        temp = Path(tempfile.mkdtemp(prefix="suixinji-l3-data-"))
        with zipfile.ZipFile(src) as zf:
            zf.extractall(temp)
        dirs = [p for p in temp.rglob("current_state_retrieval.jsonl")]
        src = dirs[0].parent if dirs else temp
    cases: list[dict[str, Any]] = []
    try:
        for name in DATA_FILES:
            path = src / name
            if not path.exists():
                matches = list(src.rglob(name))
                if matches:
                    path = matches[0]
            if not path.exists():
                raise FileNotFoundError(f"missing Layer3 dataset: {name}")
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        cases.append(json.loads(line))
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
    return cases


def _safe_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(v) for v in value]
    if hasattr(value, "__dict__"):
        return _safe_json(vars(value))
    return str(value)


def _norm(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)
    for stopword in ("用户", "您", "你", "我", "现在", "当前", "目前", "最近", "根据", "您的", "记忆", "记录"):
        text = text.replace(stopword, "")
    return text


def _iso_db(value: Any) -> Any:
    """Convert JSONL UTC ``Z`` timestamps to datetime.fromisoformat input."""
    if isinstance(value, str) and value.endswith("Z"):
        return value[:-1] + "+00:00"
    return value


def _tokens(value: Any) -> set[str]:
    text = str(value or "").casefold()
    chunks = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text)
    return set(chunks)


def _match_text(needle: Any, haystack: Any) -> bool:
    a, b = _norm(needle), _norm(haystack)
    if not a or not b:
        return False
    if a in b:
        return True
    ta, tb = _tokens(needle), _tokens(haystack)
    return bool(ta and len(ta & tb) / len(ta) >= 0.78)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return round(float(ordered[lo]), 3)
    return round(float(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)), 3)


def _prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 6), "recall": round(r, 6), "f1": round(f, 6), "tp": tp, "fp": fp, "fn": fn}


def _candidate_for(raw: dict[str, Any], note_id: str):
    from memory.models import MemoryCandidate, MEMORY_KEY_V3_VERSION

    topic = raw.get("canonical_topic") or raw.get("attribute") or raw.get("entity")
    scope = {
        "scope": raw.get("access_scope", "owner"),
        "canonical_topic": raw.get("canonical_topic"),
        "sensitivity": raw.get("sensitivity", "normal"),
        "layer3_seed": True,
    }
    return MemoryCandidate(
        memory_type=str(raw.get("memory_type") or "semantic"),
        content=str(raw.get("content") or ""),
        importance=0.8,
        confidence=0.99,
        task_status=raw.get("task_status"),
        note_id=note_id,
        subject=raw.get("entity"),
        predicate=raw.get("attribute"),
        object_value=raw.get("current_value") or raw.get("attribute") or topic,
        valid_from=_iso_db(raw.get("updated_at")),
        valid_until=None,
        memory_key=raw.get("memory_key"),
        polarity=raw.get("polarity"),
        scope=scope,
        extractor_type="layer3_seed",
        extractor_version="layer3-eval-v1",
        memory_key_version=MEMORY_KEY_V3_VERSION,
    )


def _embedding_contract_dict() -> dict[str, Any]:
    from memory.vector_lifecycle import current_embedding_contract

    model, dimension, version = current_embedding_contract()
    return {"model": model, "dimension": int(dimension), "embedding_version": version}


def _complete_seed_memory_vectors(memory_ids: list[str]) -> dict[str, Any]:
    """Materialize ready memory_vectors for isolated Layer3 seed memories."""
    from core.llm_client import embed_text
    from repositories.postgres.memory import claim_memory_vector, complete_memory_vector

    completed = 0
    skipped = 0
    failed: list[dict[str, Any]] = []
    for memory_id in memory_ids:
        claim = claim_memory_vector(memory_id)
        if claim is None:
            skipped += 1
            continue
        try:
            embedding = embed_text(str(claim.get("text") or ""))
            expected_dim = int(claim["dimension"])
            if len(embedding) != expected_dim:
                raise ValueError(f"embedding dimension mismatch: expected {expected_dim}, got {len(embedding)}")
            if complete_memory_vector(
                memory_id,
                content_hash=str(claim["content_hash"]),
                embedding=embedding,
                model=str(claim["model"]),
                dimension=expected_dim,
                embedding_version=str(claim["embedding_version"]),
            ):
                completed += 1
            else:
                failed.append({"memory_id": memory_id, "error": "complete_memory_vector returned false"})
        except Exception as exc:
            failed.append({"memory_id": memory_id, "error_type": type(exc).__name__})
    return {
        "requested": len(memory_ids),
        "completed": completed,
        "already_ready_or_inactive": skipped,
        "failed_count": len(failed),
        "failed": failed[:10],
    }


def _embed_query_for_diagnostic(query: str) -> tuple[list[float] | None, dict[str, Any]]:
    from core.llm_client import embed_text

    contract = _embedding_contract_dict()
    status = {"status": "not_attempted", "dimension": None, "contract": contract}
    if not str(query or "").strip():
        status["status"] = "empty_query"
        return None, status
    try:
        embedding = embed_text(query)
        status["dimension"] = len(embedding)
        if len(embedding) != int(contract["dimension"]):
            status["status"] = "dimension_mismatch"
            return None, status
        status["status"] = "success"
        return embedding, status
    except Exception as exc:
        status["status"] = "failed"
        status["error"] = f"{type(exc).__name__}: {exc}"
        return None, status


def _raw_vector_hits(
    space_id: str,
    query_embedding: list[float] | None,
    *,
    limit: int,
    access_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not query_embedding:
        return []
    from sqlalchemy import select
    from infrastructure.schema import Memory, MemoryVector
    from memory.access import memory_access_allowed
    from memory.vector_lifecycle import current_embedding_contract
    from repositories.postgres.memory import session_scope

    model, dimension, version = current_embedding_contract()
    distance = MemoryVector.embedding.cosine_distance(query_embedding)
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Memory, (1 - distance).label("raw_similarity"))
                .join(MemoryVector, MemoryVector.memory_id == Memory.id)
                .where(
                    Memory.space_id == space_id,
                    Memory.status == "active",
                    MemoryVector.status == "ready",
                    MemoryVector.embedding.is_not(None),
                    MemoryVector.model == model,
                    MemoryVector.dimension == int(dimension),
                    MemoryVector.embedding_version == version,
                )
                .order_by(distance, Memory.updated_at.desc())
                .limit(max(1, min(int(limit), 50)))
            )
        )
        result: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, start=1):
            memory, raw_similarity = row
            if access_context is not None and not memory_access_allowed(memory, access_context):
                continue
            result.append(
                {
                    "memory_id": str(memory.id),
                    "rank": rank,
                    "raw_similarity": round(float(raw_similarity or 0.0), 6),
                }
            )
        return result


class CaseRunner:
    def __init__(self, case: dict[str, Any], run_id: str, top_k: int):
        self.case = case
        self.run_id = run_id
        self.top_k = int(case.get("input", {}).get("top_k") or top_k)
        self.logical_to_db: dict[str, str] = {}
        self.db_to_logical: dict[str, str] = {}
        self.version_db_to_logical: dict[str, str] = {}
        self.source_note_to_logical: dict[str, str] = {}
        self.space_id = f"l3_eval_{run_id}_{case['case_id']}"
        self.seed_vector_summary: dict[str, Any] = {}

    def seed(self) -> None:
        from sqlalchemy import select
        from infrastructure.schema import MemoryDecision as MemoryDecisionRow, MemoryVersion, Space
        from repositories.postgres.common import ensure_tenant_space
        from repositories.postgres.memory import _add_source, _dt, _insert_memory, session_scope

        snapshot = self.case["input"].get("memory_snapshot") or {}
        for source in snapshot.get("sources", []) or []:
            source_ref = str(source.get("source_ref") or "")
            if source_ref:
                self.source_note_to_logical[f"layer3:{self.case['case_id']}:{source_ref}"] = source_ref
        vector_memory_ids: list[str] = []
        with session_scope() as session:
            ensure_tenant_space(session, self.space_id, tenant_id="default", source="layer3_eval", metadata={"run_id": self.run_id})
            for logical_ref, raw in ((str(m["memory_ref"]), m) for m in snapshot.get("memories", [])):
                db_id = f"l3_{self.run_id}_{self.case['case_id']}_{logical_ref}"
                self.logical_to_db[logical_ref] = db_id
                self.db_to_logical[db_id] = logical_ref
                source_refs = [str(x) for x in raw.get("source_refs") or []]
                first_note = f"layer3:{self.case['case_id']}:{source_refs[0] if source_refs else logical_ref}"
                candidate = _candidate_for(raw, first_note)
                row = _insert_memory(
                    session,
                    self.space_id,
                    candidate,
                    source_note_id=first_note,
                    source_relation="created_from",
                    status=str(raw.get("status") or "active"),
                    memory_id=db_id,
                    now=_iso_db(str(raw.get("updated_at") or self.case["input"].get("query_time") or now_iso())),
                )
                row.created_at = row.updated_at
                row.updated_at = row.updated_at
                row.last_confirmed_at = row.updated_at
                if str(raw.get("status") or "active") == "active":
                    vector_memory_ids.append(db_id)
                for source_ref in source_refs[1:]:
                    _add_source(session, db_id, f"layer3:{self.case['case_id']}:{source_ref}", "supported_by")
                versions = [v for v in snapshot.get("versions", []) if str(v.get("memory_ref")) == logical_ref]
                first = session.execute(
                    select(MemoryVersion).where(MemoryVersion.memory_id == db_id, MemoryVersion.version == 1)
                ).scalar_one_or_none()
                if versions:
                    by_seq = {int(v.get("sequence") or 1): v for v in versions}
                    for seq, version in sorted(by_seq.items()):
                        version_ref = str(version.get("version_ref") or f"{logical_ref}:v{seq}")
                        srcs = [str(x) for x in version.get("source_refs") or []]
                        note = f"layer3:{self.case['case_id']}:{srcs[0]}" if srcs else first_note
                        if seq == 1 and first is not None:
                            target = first
                        else:
                            target = MemoryVersion(
                                id=f"l3_{self.run_id}_{self.case['case_id']}_{logical_ref}_v{seq}",
                                memory_id=db_id,
                                version=seq,
                                created_at=row.updated_at,
                            )
                            session.add(target)
                        target.content = str(version.get("content") or row.content)
                        target.status = str(raw.get("status") or "active")
                        target.task_status = version.get("task_status")
                        target.confidence = row.confidence
                        target.importance = row.importance
                        target.valid_from = _dt(_iso_db(version.get("valid_from")))
                        target.valid_until = _dt(_iso_db(version.get("valid_until")))
                        target.reason = "layer3_seed"
                        target.source_note_id = note
                        self.version_db_to_logical[str(target.id)] = version_ref
                    row.current_version = max(by_seq)
                elif first is not None:
                    # The v1 stale-only fixtures describe the initial persisted
                    # version as v1 without repeating it in `versions`.  This is
                    # a seed-side ID mapping (not an answer-text/Gold inference)
                    # for the real MemoryVersion row created with the memory.
                    self.version_db_to_logical[str(first.id)] = "v1"
            # A Layer3 pending review is seeded through the same persisted
            # contract used in production: pending memory row plus a
            # memory_decisions row that links the candidate/result to targets.
            # `review_ref` remains evaluator metadata only; application code
            # sees real database ids and decision fields.
            for review in snapshot.get("pending_reviews", []) or []:
                review_ref = str(review.get("review_ref") or "review")
                refs = [str(ref) for ref in review.get("memory_refs") or []]
                db_refs = [self.logical_to_db[ref] for ref in refs if ref in self.logical_to_db]
                if not db_refs:
                    continue
                pending_ids = [
                    memory_id
                    for memory_id in db_refs
                    if str(next((raw.get("status") for raw in snapshot.get("memories", []) if str(raw.get("memory_ref")) == self.db_to_logical.get(memory_id)), "")) == "pending_review"
                ]
                result_ids = pending_ids or [db_refs[-1]]
                target_ids = [memory_id for memory_id in db_refs if memory_id not in result_ids]
                session.add(
                    MemoryDecisionRow(
                        id=f"l3_{self.run_id}_{self.case['case_id']}_{review_ref}",
                        space_id=self.space_id,
                        note_id=f"layer3:{self.case['case_id']}:{review_ref}",
                        candidate_id=f"layer3:{self.case['case_id']}:{review_ref}:candidate",
                        relation="conflict",
                        target_memory_ids_json=target_ids,
                        confidence=0.8,
                        reason=str(review.get("reason") or "pending_review"),
                        evidence_json=[{"memory_id": memory_id} for memory_id in db_refs],
                        recommended_action="pending_review",
                        status="pending_review",
                        result_memory_ids_json=result_ids,
                        error=None,
                        policy_version="layer3-eval-v1",
                        adjudicator_version="layer3-eval-v1",
                        model=None,
                        prompt_hash=None,
                        input_hash=None,
                        target_snapshot_version=None,
                        retry_of_decision_id=None,
                        created_at=_dt(_iso_db(str(self.case["input"].get("query_time") or now_iso()))),
                        applied_at=None,
                    )
                )
            session.flush()
        self.seed_vector_summary = _complete_seed_memory_vectors(vector_memory_ids)

    def db_snapshot(self) -> dict[str, Any]:
        from sqlalchemy import select
        from infrastructure.schema import Memory, MemorySource, MemoryVector, MemoryVersion
        from repositories.postgres.memory import session_scope

        with session_scope() as session:
            rows = list(session.execute(select(Memory).where(Memory.space_id == self.space_id).order_by(Memory.id)).scalars())
            result = []
            for row in rows:
                sources = list(session.execute(select(MemorySource).where(MemorySource.memory_id == row.id).order_by(MemorySource.note_id)).scalars())
                versions = list(session.execute(select(MemoryVersion).where(MemoryVersion.memory_id == row.id).order_by(MemoryVersion.version)).scalars())
                vector = session.get(MemoryVector, row.id)
                result.append({
                    "id": row.id,
                    "logical_ref": self.db_to_logical.get(row.id),
                    "memory_type": row.memory_type,
                    "content": row.content,
                    "status": row.status,
                    "task_status": row.task_status,
                    "memory_key": row.memory_key,
                    "polarity": row.polarity,
                    "current_version": row.current_version,
                    "updated_at": str(row.updated_at),
                    "access_count": row.access_count,
                    "last_accessed_at": str(row.last_accessed_at) if row.last_accessed_at else None,
                    "vector": {
                        "status": vector.status,
                        "model": vector.model,
                        "dimension": vector.dimension,
                        "embedding_version": vector.embedding_version,
                        "has_embedding": vector.embedding is not None,
                    } if vector is not None else None,
                    "source_note_ids": sorted(str(x.note_id) for x in sources),
                    "versions": [
                        {"version": v.version, "content": v.content, "status": v.status, "task_status": v.task_status,
                         "valid_from": str(v.valid_from) if v.valid_from else None, "valid_until": str(v.valid_until) if v.valid_until else None,
                         "source_note_id": v.source_note_id}
                        for v in versions
                    ],
                })
            return {"memories": result}

    def _logical_ref(self, ref: Any) -> str:
        ref_str = str(ref or "")
        if not ref_str:
            return ""
        return self.db_to_logical.get(ref_str) or self.version_db_to_logical.get(ref_str) or self.source_note_to_logical.get(ref_str) or ref_str

    def cleanup(self) -> None:
        from sqlalchemy import select
        from infrastructure.schema import Space
        from repositories.postgres.memory import session_scope

        with session_scope() as session:
            row = session.execute(select(Space).where(Space.id == self.space_id)).scalar_one_or_none()
            if row is not None:
                session.delete(row)

    def run(self) -> dict[str, Any]:
        from agent.query_agent import _deterministic_route, answer_question_result, memory_history
        from memory.service import memory_search
        from repositories.postgres.memory import hybrid_search_memory_hits

        start = time.perf_counter()
        pre: dict[str, Any] = {}
        retrieval: list[dict[str, Any]] = []
        raw_hits: list[dict[str, Any]] = []
        history_retrieval: list[dict[str, Any]] = []
        executed_channels: set[str] = set()
        answer = ""
        errors: list[dict[str, Any]] = []
        self.seed()
        if int(self.seed_vector_summary.get("failed_count") or 0) > 0:
            errors.append({
                "stage": "seed_vectors",
                "type": "SeedVectorError",
                "message": f"{self.seed_vector_summary['failed_count']} memory vectors failed to seed",
            })
        pre = self.db_snapshot()
        inp = self.case["input"]
        query = str(inp.get("query") or "")
        expected = self.case.get("expected") or {}
        try:
            route = _deterministic_route(query)
        except Exception as exc:
            route = None
            errors.append({"stage": "route_diagnostic", "type": type(exc).__name__, "message": str(exc)})
        query_embedding, query_embedding_status = _embed_query_for_diagnostic(query)
        retrieval_started = time.perf_counter()
        access_context = inp.get("access_context") or {}
        try:
            retrieval = memory_search(self.space_id, query, min_score=0.0, limit=self.top_k, access_context=access_context)
        except Exception as exc:
            errors.append({"stage": "retrieval", "type": type(exc).__name__, "message": str(exc)})
        # History is a separate production retrieval contract: search locates
        # the memory timeline, then memory_history returns version evidence.
        # Record that exposed tool result directly instead of comparing current
        # memory ids with Gold version ids in raw Recall@K.
        if route and str(route.get("action")) == "memory_history":
            try:
                history_retrieval = memory_history(
                    self.space_id,
                    query,
                    limit=self.top_k,
                    access_context=access_context,
                )
            except Exception as exc:
                errors.append({"stage": "history_retrieval", "type": type(exc).__name__, "message": str(exc)})
        retrieval_ms = (time.perf_counter() - retrieval_started) * 1000
        try:
            vector_raw_hits = {item["memory_id"]: item for item in _raw_vector_hits(self.space_id, query_embedding, limit=self.top_k, access_context=access_context)}
            for hit in hybrid_search_memory_hits(self.space_id, query, include_inactive=True, query_embedding=query_embedding, limit=self.top_k, access_context=access_context):
                executed_channels.update(name for name, rank in (("exact", hit.exact_rank), ("structured", hit.structured_rank), ("fts", hit.fts_rank), ("trigram", hit.trigram_rank), ("vector", hit.vector_rank)) if rank is not None)
                raw_hits.append({
                    "memory_id": hit.memory.id,
                    "logical_ref": self.db_to_logical.get(hit.memory.id),
                    "exact_rank": hit.exact_rank,
                    "structured_rank": hit.structured_rank,
                    "fts_rank": hit.fts_rank,
                    "trigram_rank": hit.trigram_rank,
                    "vector_rank": hit.vector_rank,
                    "vector_raw_similarity": vector_raw_hits.get(hit.memory.id, {}).get("raw_similarity"),
                    "rrf_score": hit.rrf_score,
                    "policy_score": hit.policy_score,
                    "final_score": hit.final_score,
                    "reasons": list(hit.reasons),
                })
        except Exception as exc:
            errors.append({"stage": "raw_channels", "type": type(exc).__name__, "message": str(exc)})
        answer_started = time.perf_counter()
        answer_result_payload: dict[str, Any] = {}
        try:
            structured = answer_question_result(
                self.space_id,
                query,
                max_steps=4,
                tenant_id="default",
                # Keep ACL semantics from access_context, but isolate evaluator
                # traffic from the production per-user ask quota.
                user_id=f"layer3_eval:{self.run_id}:{self.case['case_id']}",
                message_id=f"layer3:{self.run_id}:{self.case['case_id']}",
                task_id=f"layer3:{self.run_id}:{self.case['case_id']}",
                access_context=access_context,
            )
            answer_result_payload = _safe_json(structured.to_dict())
            answer = str(structured.answer or "")
            if structured.answer_type == "system_error":
                errors.append({"stage": "answer", "type": "StructuredAnswerError", "message": structured.reason_code})
        except Exception as exc:
            errors.append({"stage": "answer", "type": type(exc).__name__, "message": str(exc)})
            answer = ""
        answer_ms = (time.perf_counter() - answer_started) * 1000
        post = self.db_snapshot()
        route_name = "complex"
        if route:
            action = str(route.get("action"))
            if action == "semantic_search":
                route_name = "vector"
            elif action == "memory_search":
                route_name = "structured" if route.get("args", {}).get("memory_type") else "hybrid"
            elif action in {"filter_notes", "list_recent", "list_tasks", "list_recent_episodes", "profile_summary"}:
                route_name = "structured"
            elif action == "memory_history":
                route_name = "history"
        sources = [str(x) for x in re.findall(r"memory:([^｜\s]+)", answer or "")]
        retrieved = []
        for item in retrieval:
            db_id = str(item.get("id"))
            retrieved.append(self.db_to_logical.get(db_id, f"db:{db_id}"))
        history_retrieved = [self._logical_ref(item.get("id") or item.get("version_id")) for item in history_retrieval]
        cited = [self.db_to_logical.get(x, x) for x in sources]
        answer_selected_memory_refs = [self._logical_ref(item) for item in (answer_result_payload.get("selected_memory_ids") or [])]
        answer_selected_version_refs = [self._logical_ref(item) for item in (answer_result_payload.get("selected_version_ids") or [])]
        answer_selected_source_refs = [self._logical_ref(item) for item in (answer_result_payload.get("selected_source_ids") or [])]
        answer_selected_context_refs = [self._logical_ref(item) for item in (answer_result_payload.get("selected_context_refs") or [])]
        answer_selected_tool_refs = [str(item) for item in (answer_result_payload.get("selected_tool_refs") or [])]
        answer_executed_tools = [str(item) for item in (answer_result_payload.get("executed_tools") or [])]
        answer_evidence_bundle = _safe_json(answer_result_payload.get("evidence_bundle"))
        # Empty lists are legitimate production evidence (for example
        # no_answer/restricted); only a missing structured AnswerResult is
        # unavailable. Do not infer any values from answer text or Gold.
        answer_contract_exposed = bool(answer_result_payload) and "evidence_bundle" in answer_result_payload
        # Evaluator adapter only: map production ids from the already exposed
        # structured claims. It never infers claims from answer text or Gold.
        answer_structured_claims = [
            {
                "text": str(claim.get("text") or ""),
                "memory_refs": [self._logical_ref(item) for item in claim.get("memory_ids") or []],
                "version_refs": [self._logical_ref(item) for item in claim.get("version_ids") or []],
                "source_refs": [self._logical_ref(item) for item in claim.get("source_ids") or []],
                "support_role": claim.get("support_role"),
            }
            for claim in (answer_result_payload.get("claims") or [])
            if isinstance(claim, dict)
        ]
        def map_claim(claim: dict[str, Any]) -> dict[str, Any]:
            return {
                "text": str(claim.get("text") or ""),
                "claim_id": claim.get("claim_id"),
                "memory_refs": [self._logical_ref(item) for item in claim.get("memory_ids") or []],
                "version_refs": [self._logical_ref(item) for item in claim.get("version_ids") or []],
                "source_refs": [self._logical_ref(item) for item in claim.get("source_ids") or []],
                "support_role": claim.get("support_role"),
            }
        answer_claim_groups = []
        for group in answer_result_payload.get("claim_groups") or []:
            if not isinstance(group, dict):
                continue
            summary = group.get("summary_claim")
            members = group.get("member_claims") or []
            answer_claim_groups.append({
                "group_type": group.get("group_type"),
                "summary_claim": map_claim(summary) if isinstance(summary, dict) else {},
                "ordered_member_claim_ids": [str(item) for item in group.get("ordered_member_claim_ids") or []],
                "member_claims": [map_claim(item) for item in members if isinstance(item, dict)],
                "memory_refs": [self._logical_ref(item) for item in group.get("memory_ids") or []],
                "version_refs": [self._logical_ref(item) for item in group.get("version_ids") or []],
                "source_refs": [self._logical_ref(item) for item in group.get("source_ids") or []],
                "support_role": group.get("support_role"),
            })
        source_refs_by_memory = {
            str(m.get("memory_ref")): [str(x) for x in (m.get("source_refs") or [])]
            for m in (inp.get("memory_snapshot") or {}).get("memories", [])
        }
        # Structured selected sources are the production contract. Text source
        # lines are presentation-only and may be truncated, so use them solely
        # as a legacy fallback when the production result has no source IDs.
        cited_source_refs = sorted(set(answer_selected_source_refs))
        if not cited_source_refs:
            cited_source_refs = sorted({ref for logical in cited for ref in source_refs_by_memory.get(logical, [])})
        return {
            "case_id": self.case["case_id"],
            "dataset": self.case.get("dataset"),
            "difficulty": self.case.get("difficulty"),
            "coverage_tags": self.case.get("coverage_tags") or [],
            "space_id": self.space_id,
            "query": query,
            "query_time": inp.get("query_time"),
            "access_context": inp.get("access_context") or {},
            "query_embedding_status": query_embedding_status,
            "expected_route": expected.get("expected_route"),
            "observed_route_diagnostic": route_name,
            "observed_route_detail": _safe_json(route),
            "retrieval": _safe_json(retrieval),
            "retrieved_refs": retrieved,
            "history_retrieval": _safe_json(history_retrieval),
            "history_retrieved_refs": history_retrieved,
            # Stage 0 contract: do not infer selected context/tool evidence from
            # answer text, Gold, route diagnostics, or extra lookups. Empty lists
            # are valid exposed evidence for no_answer/restricted responses.
            "selected_context_refs": answer_selected_context_refs if answer_contract_exposed else None,
            "selected_context_ref_status": "available" if answer_contract_exposed else "unavailable_until_stage1_evidence_bundle",
            "selected_tool_refs": answer_selected_tool_refs if answer_contract_exposed else None,
            "selected_tool_ref_status": "available" if answer_contract_exposed else "unavailable_until_stage1_evidence_bundle",
            "executed_tools": answer_executed_tools if answer_contract_exposed else None,
            "executed_tools_status": "available" if answer_contract_exposed else "unavailable_until_stage1_evidence_bundle",
            "raw_channel_hits": raw_hits,
            "executed_channels": sorted(executed_channels),
            "answer": answer,
            "answer_result": answer_result_payload,
            "answer_selected_memory_refs": answer_selected_memory_refs,
            "answer_selected_version_refs": answer_selected_version_refs,
            "answer_selected_source_refs": answer_selected_source_refs,
            "answer_selected_context_refs": answer_selected_context_refs,
            "answer_selected_tool_refs": answer_selected_tool_refs,
            "answer_executed_tools": answer_executed_tools,
            "answer_structured_claims": answer_structured_claims,
            "answer_claim_groups": answer_claim_groups,
            "answer_evidence_bundle": answer_evidence_bundle,
            "answer_memory_citations": cited,
            "answer_source_citations": cited_source_refs,
            "answer_citation_lines": [x for x in answer.splitlines() if "memory:" in x],
            "expected": expected,
            "memory_snapshot_input": inp.get("memory_snapshot") or {},
            "pre_snapshot": pre,
            "post_snapshot": post,
            "seed_vector_summary": self.seed_vector_summary,
            "errors": errors,
            "latency_ms": {"retrieval": round(retrieval_ms, 3), "answer": round(answer_ms, 3), "total": round((time.perf_counter() - start) * 1000, 3)},
        }


def _answer_no(answer: str) -> bool:
    text = str(answer or "")
    return any(token in text for token in ("没有足够信息", "没有找到足够", "无法回答", "没有相关记忆", "暂无足够"))


def _state_changed(pre: dict[str, Any], post: dict[str, Any]) -> bool:
    def essential(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if k not in {"updated_at", "access_count", "last_accessed_at"}}

    return [essential(x) for x in pre.get("memories", [])] != [essential(x) for x in post.get("memories", [])]


def _parse_time(value: Any):
    if not value:
        return None
    try:
        from datetime import datetime, timezone

        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _snapshot_maps(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    memories = {str(m.get("memory_ref")): m for m in (snapshot or {}).get("memories", []) if m.get("memory_ref")}
    versions = {str(v.get("version_ref")): v for v in (snapshot or {}).get("versions", []) if v.get("version_ref")}
    sources = {str(s.get("source_ref")): s for s in (snapshot or {}).get("sources", []) if s.get("source_ref")}
    return {"memories": memories, "versions": versions, "sources": sources}


def _is_sensitive_ref(ref: str, maps: dict[str, dict[str, Any]]) -> bool:
    memory = maps["memories"].get(str(ref))
    if memory is not None:
        return str(memory.get("sensitivity") or "normal") != "normal"
    version = maps["versions"].get(str(ref))
    if version is not None:
        parent = maps["memories"].get(str(version.get("memory_ref") or ""))
        return bool(parent and str(parent.get("sensitivity") or "normal") != "normal")
    return False


def _is_stale_ref(ref: str, maps: dict[str, dict[str, Any]], query_time: Any) -> bool:
    ref = str(ref)
    memory = maps["memories"].get(ref)
    if memory is not None:
        status = str(memory.get("status") or "active")
        if status in {"superseded", "expired", "archived", "forgotten", "deleted"}:
            return True
        valid_until = _parse_time(memory.get("valid_until"))
        qtime = _parse_time(query_time)
        if valid_until is not None and qtime is not None and valid_until < qtime:
            return True
        return False
    version = maps["versions"].get(ref)
    if version is not None:
        valid_until = _parse_time(version.get("valid_until"))
        qtime = _parse_time(query_time)
        if valid_until is not None and qtime is not None and valid_until < qtime:
            return True
        parent = maps["memories"].get(str(version.get("memory_ref") or ""))
        return bool(parent and str(parent.get("status") or "active") in {"superseded", "expired", "archived", "forgotten", "deleted"})
    return False


def _is_ambiguous_case(pred: dict[str, Any], expected: dict[str, Any]) -> bool:
    return (
        str(expected.get("answer_type") or "") == "clarification"
        or bool(expected.get("ambiguous_candidate") or expected.get("requires_clarification"))
        or "ambiguous_reference" in set(pred.get("coverage_tags") or [])
    )


def score_case(pred: dict[str, Any]) -> dict[str, Any]:
    expected = pred.get("expected") or {}
    answer_result = pred.get("answer_result") or {}
    structured_type = str(answer_result.get("answer_type") or "")
    expected_answer_type = str(expected.get("answer_type") or "")
    retrieved = pred.get("retrieved_refs") or []
    selected_status = str(pred.get("selected_context_ref_status") or "")
    selected_refs = pred.get("selected_context_refs")
    judged_refs = set(selected_refs or []) if selected_status == "available" else set(retrieved)
    relevant_current = set(expected.get("relevant_current_refs") or [])
    relevant_history = set(expected.get("relevant_history_refs") or [])
    relevant = relevant_current | relevant_history
    must_not = set(expected.get("must_not_return_refs") or [])
    maps = _snapshot_maps(pred.get("memory_snapshot_input") or {})
    retrieved_set = set(retrieved)
    must_not_hits = set() if expected_answer_type == "clarification" else judged_refs & must_not
    ambiguous_case = _is_ambiguous_case(pred, expected)
    stale_refs = {ref for ref in judged_refs if _is_stale_ref(ref, maps, pred.get("query_time"))}
    sensitive_refs = {ref for ref in maps["memories"] if _is_sensitive_ref(ref, maps)}
    ambiguous_refs = (judged_refs - relevant) if ambiguous_case else set()
    irrelevant_refs = judged_refs - relevant - stale_refs - sensitive_refs - ambiguous_refs
    ranks = {ref: i + 1 for i, ref in enumerate(retrieved)}
    history_retrieved = pred.get("history_retrieved_refs") or []
    history_ranks = {ref: i + 1 for i, ref in enumerate(history_retrieved)}
    rank_metrics: dict[str, Any] = {}
    for k in (1, 3, 5, 10):
        hit_refs = set(retrieved[:k])
        rank_metrics[str(k)] = {
            "precision": round(len(hit_refs & relevant) / k, 6),
            "recall": round(len(hit_refs & relevant) / len(relevant), 6) if relevant else None,
            "hit": bool(hit_refs & relevant),
            "current_hit": bool(hit_refs & relevant_current),
            "history_hit": bool(hit_refs & relevant_history),
        }
    history_version_rank: dict[str, Any] = {}
    for k in (1, 3, 5, 10):
        hit_refs = set(history_retrieved[:k])
        history_version_rank[str(k)] = {
            "precision": round(len(hit_refs & relevant_history) / k, 6),
            "recall": round(len(hit_refs & relevant_history) / len(relevant_history), 6) if relevant_history else None,
            "hit": bool(hit_refs & relevant_history),
        }
    evidence_mode = str(expected.get("evidence_mode") or "")
    ordinary_retrieval_eligible = (
        expected_answer_type == "answered"
        and evidence_mode in {"current", "mixed"}
        and bool(relevant_current)
    )
    # qualified_history_only is a safety response to a *current* question
    # whose only evidence is stale.  It must remain in stale/no-answer safety
    # reporting, not be credited as a direct memory_history version lookup.
    history_version_eligible = (
        expected_answer_type == "answered"
        and evidence_mode == "history"
        and bool(relevant_history)
    )
    first_rank = min((ranks[x] for x in relevant if x in ranks), default=None)
    graded = expected.get("graded_relevance") or {}
    dcg = sum(float(graded.get(ref, 0)) / math.log2(i + 2) for i, ref in enumerate(retrieved[:10]))
    ideal = sorted((float(v) for v in graded.values()), reverse=True)[:10]
    idcg = sum(v / math.log2(i + 2) for i, v in enumerate(ideal))
    stale_retrieved = bool(stale_refs)
    must_not_return_violation = bool(must_not_hits)
    irrelevant_retrieved = bool(irrelevant_refs)
    ambiguous_candidate = ambiguous_case and bool(ambiguous_refs)
    snap_mem = maps["memories"]
    content_by_ref = {ref: str(m.get("content") or "") for ref, m in snap_mem.items()}
    for ref, version in maps["versions"].items():
        content_by_ref[ref] = str(version.get("content") or "")
    answer = str(pred.get("answer") or "")
    claims = expected.get("expected_claims") or []
    expected_groups = expected.get("expected_claim_groups") or []
    matched_claims = []
    structured_claims = [claim for claim in pred.get("answer_structured_claims") or [] if isinstance(claim, dict) and str(claim.get("text") or "")]
    group_score: dict[str, Any] | None = None
    if expected_answer_type in NON_FACT_ANSWER_TYPES or structured_type in NON_FACT_ANSWER_TYPES:
        answer_prf = _prf(0, len(structured_claims), len(claims))
    elif expected_groups:
        produced_groups = [group for group in pred.get("answer_claim_groups") or [] if isinstance(group, dict)]
        matched_groups = 0
        order_correct = 0
        version_source_correct = 0
        for expected_group in expected_groups:
            if not isinstance(expected_group, dict):
                continue
            target_versions = [str(item) for item in expected_group.get("version_refs") or []]
            target_sources = set(str(item) for item in expected_group.get("source_refs") or [])
            target_summary = (expected_group.get("summary_claim") or {}).get("claim")
            for produced_group in produced_groups:
                if produced_group.get("group_type") != expected_group.get("group_type"):
                    continue
                actual_versions = [str(item) for item in produced_group.get("version_refs") or []]
                actual_sources = set(str(item) for item in produced_group.get("source_refs") or [])
                actual_summary = (produced_group.get("summary_claim") or {}).get("text")
                if target_versions != actual_versions or target_sources != actual_sources:
                    continue
                if target_summary and not _match_text(target_summary, actual_summary):
                    continue
                matched_groups += 1
                order_correct += 1
                version_source_correct += 1
                break
        answer_prf = _prf(matched_groups, max(0, len(produced_groups) - matched_groups), max(0, len(expected_groups) - matched_groups))
        group_score = {
            "groups": answer_prf,
            "matched": matched_groups,
            "expected": len(expected_groups),
            "timeline_order_accuracy": round(order_correct / len(expected_groups), 6) if expected_groups else 0.0,
            "version_source_exact": round(version_source_correct / len(expected_groups), 6) if expected_groups else 0.0,
        }
    elif structured_claims:
        for claim in claims:
            expected_refs = set(claim.get("memory_refs") or []) | set(claim.get("version_refs") or [])
            expected_sources = set(claim.get("source_refs") or [])
            for produced in structured_claims:
                produced_refs = set(produced.get("memory_refs") or []) | set(produced.get("version_refs") or [])
                produced_sources = set(produced.get("source_refs") or [])
                if _match_text(claim.get("claim"), produced.get("text")) or (
                    expected_refs and expected_refs <= produced_refs and (not expected_sources or expected_sources <= produced_sources)
                ):
                    matched_claims.append(claim)
                    break
        answer_prf = _prf(len(matched_claims), max(0, len(structured_claims) - len(matched_claims)), max(0, len(claims) - len(matched_claims)))
    else:
        for claim in claims:
            if _match_text(claim.get("claim"), answer):
                matched_claims.append(claim)
                continue
            refs = set(claim.get("memory_refs") or []) | set(claim.get("version_refs") or [])
            if any(_match_text(content_by_ref.get(ref), answer) for ref in refs if content_by_ref.get(ref)):
                matched_claims.append(claim)
        predicted_sentences = [x.strip() for x in re.split(r"[。！？!?\n]+", answer) if x.strip() and "来源" not in x and "memory:" not in x]
        answer_prf = _prf(len(matched_claims), max(0, len(predicted_sentences) - len(matched_claims)), max(0, len(claims) - len(matched_claims)))
    required_sources = set(expected.get("required_citation_refs") or [])
    if expected_answer_type in NON_FACT_ANSWER_TYPES or structured_type in NON_FACT_ANSWER_TYPES:
        cited = set()
    else:
        cited = set(pred.get("answer_source_citations") or [])
    citation_scored_cited = set() if expected_answer_type in NON_FACT_ANSWER_TYPES or structured_type in NON_FACT_ANSWER_TYPES else cited
    cite_prf = _prf(
        len(citation_scored_cited & required_sources),
        len(citation_scored_cited - required_sources),
        len(required_sources - citation_scored_cited),
    )
    forbidden = [str(x) for x in expected.get("forbidden_claims") or []]
    forbidden_hit = any(_match_text(x, answer) for x in forbidden)
    stale_snapshot_refs = {ref for ref in content_by_ref if _is_stale_ref(ref, maps, pred.get("query_time"))}
    stale_answer = any(_match_text(content_by_ref.get(ref), answer) for ref in stale_snapshot_refs if content_by_ref.get(ref)) and not (
        str((pred.get("answer_result") or {}).get("answer_type") or "") == "qualified_history_only"
        or str((pred.get("answer_result") or {}).get("reason_code") or "") in {"history_query", "history_timeline"}
    )
    requester = (pred.get("access_context") or {}).get("requester")
    allow_sensitive = bool((pred.get("access_context") or {}).get("allow_sensitive"))
    sensitive_returned = (judged_refs | cited) & sensitive_refs
    sensitive_answer_leak = any(_match_text(content_by_ref.get(ref), answer) for ref in sensitive_refs if content_by_ref.get(ref))
    access_violation = bool(sensitive_returned and (requester != "owner" or not allow_sensitive)) or bool(sensitive_answer_leak and (requester != "owner" or not allow_sensitive))
    expected_no = expected_answer_type == "no_answer"
    restricted_predicted = structured_type == "restricted"
    restricted_expected = bool(sensitive_refs and (requester != "owner" or not allow_sensitive))
    predicted_no = structured_type == "no_answer" or (not structured_type and _answer_no(answer))
    no_tp = int(expected_no and predicted_no)
    no_fp = int(not expected_no and predicted_no)
    no_fn = int(expected_no and not predicted_no)
    no_tn = int(not expected_no and not predicted_no)
    return {
        "case_id": pred.get("case_id"), "dataset": pred.get("dataset"), "tags": pred.get("coverage_tags") or [],
        "retrieval": {"rank": rank_metrics, "history_version_rank": history_version_rank,
                       "ordinary_retrieval_eligible": ordinary_retrieval_eligible,
                       "history_version_eligible": history_version_eligible,
                       "mrr": round(1 / first_rank, 6) if first_rank else 0.0, "ndcg_at_10": round(dcg / idcg, 6) if idcg else None,
                       "stale_retrieval": stale_retrieved, "stale_retrieval_violation": stale_retrieved, "stale_refs": sorted(stale_refs),
                       "must_not_return_violation": must_not_return_violation, "must_not_return_refs": sorted(must_not_hits),
                       "irrelevant_retrieval": irrelevant_retrieved, "irrelevant_refs": sorted(irrelevant_refs),
                       "ambiguous_candidate": ambiguous_candidate, "ambiguous_candidate_usage": ambiguous_candidate, "ambiguous_candidate_refs": sorted(ambiguous_refs)},
        "answer": {"claims": answer_prf, "claim_groups": group_score, "matched_claims": len(matched_claims), "expected_claims": len(claims), "forbidden_claim_hit": forbidden_hit, "stale_used": stale_answer, "stale_answer_usage": stale_answer, "answer_type": structured_type or ("no_answer" if predicted_no else "answered"), "reason_code": answer_result.get("reason_code"), "restricted_predicted": restricted_predicted, "restricted_expected": restricted_expected},
        "no_answer": {"expected": expected_no, "predicted": predicted_no, "tp": no_tp, "fp": no_fp, "fn": no_fn, "tn": no_tn, **_prf(no_tp, no_fp, no_fn)},
        "citation": {"required": sorted(required_sources), "actual": sorted(citation_scored_cited), "exact_set": citation_scored_cited == required_sources, **cite_prf},
        "access": {"violation": access_violation, "sensitive_answer_leak": sensitive_answer_leak, "sensitive_refs": sorted(sensitive_refs), "retrieved_sensitive_refs": sorted(judged_refs & sensitive_refs), "must_not_return": bool(expected.get("must_not_return_refs")), "restricted_expected": restricted_expected, "restricted_predicted": restricted_predicted},
        "stage0_contract": {
            "selected_context_refs_status": pred.get("selected_context_ref_status") or ("available" if pred.get("selected_context_refs") is not None else "unavailable"),
            "selected_tool_refs_status": pred.get("selected_tool_ref_status") or ("available" if pred.get("selected_tool_refs") is not None else "unavailable"),
            "executed_tools_status": pred.get("executed_tools_status") or ("available" if pred.get("executed_tools") is not None else "unavailable"),
        },
        "read_only": {"business_state_changed": _state_changed(pred.get("pre_snapshot") or {}, pred.get("post_snapshot") or {})},
        "errors": pred.get("errors") or [],
    }


def aggregate(scored: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in scored:
        by_dataset[str(item.get("dataset"))].append(item)

    def rank_summary(items: list[dict[str, Any]], *, rank_key: str, include: str | None = None) -> dict[str, Any]:
        """Aggregate only a homogeneous retrieval contract.

        Current factual recall, history-version recall, and negative safety
        cases have different correct outcomes.  Never average them together.
        """
        if include is not None:
            items = [item for item in items if bool(item["retrieval"].get(include))]
        out: dict[str, Any] = {"cases": len(items)}
        for k in (1, 3, 5, 10):
            values = [item["retrieval"][rank_key][str(k)] for item in items]
            recalls = [value["recall"] for value in values if value["recall"] is not None]
            out[f"precision@{k}"] = round(statistics.mean(value["precision"] for value in values), 6) if values else None
            out[f"recall@{k}"] = round(statistics.mean(recalls), 6) if recalls else None
            out[f"hit@{k}"] = round(statistics.mean(1.0 if value["hit"] else 0.0 for value in values), 6) if values else None
        return out

    def aggregate_group(items: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"cases": len(items)}
        for k in (1, 3, 5, 10):
            vals = [x["retrieval"]["rank"][str(k)] for x in items]
            out[f"precision@{k}"] = round(statistics.mean(x["precision"] for x in vals), 6) if vals else 0.0
            recalls = [x["recall"] for x in vals if x["recall"] is not None]
            out[f"recall@{k}"] = round(statistics.mean(recalls), 6) if recalls else None
            out[f"hit@{k}"] = round(statistics.mean(1.0 if x["hit"] else 0.0 for x in vals), 6) if vals else 0.0
            out[f"current_hit@{k}"] = round(statistics.mean(1.0 if x["current_hit"] else 0.0 for x in vals), 6) if vals else 0.0
            out[f"history_hit@{k}"] = round(statistics.mean(1.0 if x["history_hit"] else 0.0 for x in vals), 6) if vals else 0.0
        out["mrr"] = round(statistics.mean(x["retrieval"]["mrr"] for x in items), 6) if items else 0.0
        ndcg = [x["retrieval"]["ndcg_at_10"] for x in items if x["retrieval"]["ndcg_at_10"] is not None]
        out["ndcg@10"] = round(statistics.mean(ndcg), 6) if ndcg else None
        claim_counts = [x["answer"]["claims"] for x in items]
        out["answer_claims"] = _prf(sum(x["tp"] for x in claim_counts), sum(x["fp"] for x in claim_counts), sum(x["fn"] for x in claim_counts))
        out["answer_type_counts"] = dict(Counter(str(x["answer"].get("answer_type") or "unknown") for x in items))
        quality_items = [x for x in items if x["answer"].get("answer_type") not in {"system_error", "no_answer", "restricted"}]
        out["successful_call_quality_cases"] = len(quality_items)
        out["answer_quality_on_success"] = _prf(
            sum(x["answer"]["claims"]["tp"] for x in quality_items),
            sum(x["answer"]["claims"]["fp"] for x in quality_items),
            sum(x["answer"]["claims"]["fn"] for x in quality_items),
        )
        out["end_to_end_answer_success_rate"] = round(statistics.mean(1.0 if x["answer"]["claims"]["f1"] >= 0.5 and not x["answer"]["forbidden_claim_hit"] else 0.0 for x in items), 6) if items else 0.0
        no = [x["no_answer"] for x in items]
        out["no_answer"] = {"tp": sum(x["tp"] for x in no), "fp": sum(x["fp"] for x in no), "fn": sum(x["fn"] for x in no), "tn": sum(x["tn"] for x in no)}
        out["no_answer"].update(_prf(out["no_answer"]["tp"], out["no_answer"]["fp"], out["no_answer"]["fn"]))
        cites = [x["citation"] for x in items]
        out["citation"] = _prf(sum(x["tp"] for x in cites), sum(x["fp"] for x in cites), sum(x["fn"] for x in cites))
        out["citation_exact_set_rate"] = round(statistics.mean(1.0 if x["exact_set"] else 0.0 for x in cites), 6) if cites else 0.0
        out["stale_retrieval_rate"] = round(statistics.mean(1.0 if x["retrieval"]["stale_retrieval"] else 0.0 for x in items), 6) if items else 0.0
        out["stale_retrieval_violation_rate"] = round(statistics.mean(1.0 if x["retrieval"].get("stale_retrieval_violation") else 0.0 for x in items), 6) if items else 0.0
        out["must_not_return_violation_rate"] = round(statistics.mean(1.0 if x["retrieval"].get("must_not_return_violation") else 0.0 for x in items), 6) if items else 0.0
        out["irrelevant_retrieval_rate"] = round(statistics.mean(1.0 if x["retrieval"].get("irrelevant_retrieval") else 0.0 for x in items), 6) if items else 0.0
        out["ambiguous_candidate_rate"] = round(statistics.mean(1.0 if x["retrieval"].get("ambiguous_candidate") else 0.0 for x in items), 6) if items else 0.0
        out["ambiguous_candidate_usage_rate"] = round(statistics.mean(1.0 if x["retrieval"].get("ambiguous_candidate_usage") else 0.0 for x in items), 6) if items else 0.0
        out["stale_answer_use_rate"] = round(statistics.mean(1.0 if x["answer"]["stale_used"] else 0.0 for x in items), 6) if items else 0.0
        out["stale_answer_usage_rate"] = round(statistics.mean(1.0 if x["answer"].get("stale_answer_usage") else 0.0 for x in items), 6) if items else 0.0
        out["forbidden_claim_rate"] = round(statistics.mean(1.0 if x["answer"]["forbidden_claim_hit"] else 0.0 for x in items), 6) if items else 0.0
        out["access_violation_count"] = sum(1 for x in items if x["access"]["violation"])
        out["restricted_expected_count"] = sum(1 for x in items if x["access"].get("restricted_expected"))
        out["restricted_predicted_count"] = sum(1 for x in items if x["access"].get("restricted_predicted"))
        out["restricted_answer_rate"] = round(statistics.mean(1.0 if x["access"].get("restricted_predicted") else 0.0 for x in items), 6) if items else 0.0
        out["selected_context_unavailable_rate"] = round(statistics.mean(1.0 if x.get("stage0_contract", {}).get("selected_context_refs_status") != "available" else 0.0 for x in items), 6) if items else 0.0
        out["selected_tool_refs_unavailable_rate"] = round(statistics.mean(1.0 if x.get("stage0_contract", {}).get("selected_tool_refs_status") != "available" else 0.0 for x in items), 6) if items else 0.0
        out["executed_tools_unavailable_rate"] = round(statistics.mean(1.0 if x.get("stage0_contract", {}).get("executed_tools_status") != "available" else 0.0 for x in items), 6) if items else 0.0
        out["business_state_mutation_count"] = sum(1 for x in items if x["read_only"]["business_state_changed"])
        # The original top-level rank metrics remain raw diagnostics for
        # backwards comparison.  These two explicit metrics are the scoring
        # contract for ordinary factual retrieval and timeline version lookup.
        out["ordinary_current_retrieval"] = rank_summary(
            items,
            rank_key="rank",
            include="ordinary_retrieval_eligible",
        )
        out["history_version_retrieval"] = rank_summary(
            items,
            rank_key="history_version_rank",
            include="history_version_eligible",
        )
        return out

    metrics = {"overall": aggregate_group(scored), "by_dataset": {name: aggregate_group(items) for name, items in sorted(by_dataset.items())}}
    tags: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in scored:
        for tag in item.get("tags") or []:
            tags[str(tag)].append(item)
    metrics["by_coverage_tag"] = {tag: aggregate_group(items) for tag, items in sorted(tags.items())}
    semantic_predictions = [
        pred for pred in predictions
        if pred.get("dataset") == "semantic_paraphrase_and_noise" or "semantic" in set(pred.get("coverage_tags") or [])
    ]
    seed_total = 0
    seed_ready = 0
    contract_total = 0
    contract_matched = 0
    query_embedding_success = 0
    recall_tags = ("paraphrase", "typo", "noise", "mixed_language")
    recall_by_tag: dict[str, dict[str, Any]] = {}
    for pred in semantic_predictions:
        contract = (pred.get("query_embedding_status") or {}).get("contract") or {}
        if (pred.get("query_embedding_status") or {}).get("status") == "success":
            query_embedding_success += 1
        for memory in (pred.get("pre_snapshot") or {}).get("memories", []):
            if str(memory.get("status") or "") != "active":
                continue
            seed_total += 1
            vector = memory.get("vector") or {}
            ready = vector.get("status") == "ready" and bool(vector.get("has_embedding"))
            if ready:
                seed_ready += 1
            if vector:
                contract_total += 1
                if (
                    vector.get("model") == contract.get("model")
                    and int(vector.get("dimension") or 0) == int(contract.get("dimension") or 0)
                    and vector.get("embedding_version") == contract.get("embedding_version")
                ):
                    contract_matched += 1
        expected = pred.get("expected") or {}
        relevant = set(expected.get("relevant_current_refs") or []) | set(expected.get("relevant_history_refs") or [])
        got = set((pred.get("retrieved_refs") or [])[:3])
        for tag in recall_tags:
            if tag not in set(pred.get("coverage_tags") or []):
                continue
            bucket = recall_by_tag.setdefault(tag, {"cases": 0, "hits": 0, "recall@3": 0.0})
            bucket["cases"] += 1
            if relevant and relevant & got:
                bucket["hits"] += 1
    for bucket in recall_by_tag.values():
        bucket["recall@3"] = round(bucket["hits"] / bucket["cases"], 6) if bucket["cases"] else 0.0
    metrics["stage2_semantic_retrieval"] = {
        "cases": len(semantic_predictions),
        "vector_seed_ready_rate": round(seed_ready / seed_total, 6) if seed_total else None,
        "vector_seed_ready_count": seed_ready,
        "vector_seed_expected_count": seed_total,
        "embedding_contract_match_rate": round(contract_matched / contract_total, 6) if contract_total else None,
        "embedding_contract_matched_count": contract_matched,
        "embedding_contract_checked_count": contract_total,
        "query_embedding_success_rate": round(query_embedding_success / len(semantic_predictions), 6) if semantic_predictions else None,
        "query_embedding_success_count": query_embedding_success,
        "recall_by_tag": recall_by_tag,
    }
    return metrics


def write_reports(out_dir: Path, predictions: list[dict[str, Any]], scored: list[dict[str, Any]], started: str, args: argparse.Namespace) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = aggregate(scored, predictions)
    all_errors = [e for p in predictions for e in (p.get("errors") or [])]
    answer_errors = [e for e in all_errors if e.get("stage") == "answer"]
    execution_errors = [e for e in all_errors if e.get("stage") != "answer"]
    metrics["overall"]["answer_error_count"] = len(answer_errors)
    metrics["overall"]["execution_error_count"] = len(execution_errors)
    metrics["overall"]["answer_error_types"] = dict(Counter(str(e.get("type")) for e in answer_errors))
    retrieval = {"overall": metrics["overall"], "by_dataset": metrics["by_dataset"], "by_coverage_tag": metrics["by_coverage_tag"], "stage2_semantic_retrieval": metrics.get("stage2_semantic_retrieval", {})}
    answer = {"overall": metrics["overall"], "by_dataset": metrics["by_dataset"]}
    citation = {"overall": metrics["overall"], "by_dataset": metrics["by_dataset"]}
    answer_availability = {
        "overall": {"type_counts": metrics["overall"].get("answer_type_counts", {}), "no_answer": metrics["overall"]["no_answer"], "system_error_count": metrics["overall"].get("answer_type_counts", {}).get("system_error", 0)},
        "by_dataset": {k: {"type_counts": v.get("answer_type_counts", {}), "no_answer": v["no_answer"]} for k, v in metrics["by_dataset"].items()},
    }
    answer_quality = {
        "overall": {"successful_call_quality": metrics["overall"].get("answer_quality_on_success"), "end_to_end_answer_success_rate": metrics["overall"].get("end_to_end_answer_success_rate")},
        "by_dataset": {k: {"successful_call_quality": v.get("answer_quality_on_success"), "end_to_end_answer_success_rate": v.get("end_to_end_answer_success_rate")} for k, v in metrics["by_dataset"].items()},
    }
    no_answer = {"overall": metrics["overall"]["no_answer"], "by_dataset": {k: v["no_answer"] for k, v in metrics["by_dataset"].items()}}
    access = {
        "access_violation_count": metrics["overall"]["access_violation_count"],
        "cross_space_violation_count": sum(1 for p in predictions for x in p.get("retrieval", []) if x.get("space_id") != p.get("space_id")),
        "business_state_mutation_count": metrics["overall"]["business_state_mutation_count"],
        "restricted_expected_count": metrics["overall"].get("restricted_expected_count", 0),
        "restricted_predicted_count": metrics["overall"].get("restricted_predicted_count", 0),
        "restricted_answer_rate": metrics["overall"].get("restricted_answer_rate", 0.0),
    }
    route = Counter((str(p.get("expected_route")), str(p.get("observed_route_diagnostic"))) for p in predictions)
    route_report = {"confusion": [{"expected": a, "observed": b, "count": n} for (a, b), n in sorted(route.items())]}
    latency_values = [p.get("latency_ms", {}) for p in predictions]
    latency = {name: {"p50": _pct([float(x.get(name, 0)) for x in latency_values], .5), "p95": _pct([float(x.get(name, 0)) for x in latency_values], .95), "p99": _pct([float(x.get(name, 0)) for x in latency_values], .99)} for name in ("retrieval", "answer", "total")}
    failures = [p for p in predictions if p.get("errors") or score_case(p)["read_only"]["business_state_changed"] or score_case(p)["retrieval"]["must_not_return_violation"] or score_case(p)["answer"]["forbidden_claim_hit"]]
    manifest = {"schema_version": "suixinji.layer3.run.v1", "started_at": started, "finished_at": now_iso(), "run_id": args.run_id, "backend": os.getenv("STORAGE_BACKEND"), "retrieval_mode": os.getenv("SUIXINJI_MEMORY_RETRIEVAL_MODE"), "dataset_dir": args.data_dir, "case_count": len(predictions), "concurrency": args.concurrency, "top_k": args.top_k, "production_entry": ["memory.service.memory_search", "agent.query_agent.answer_question_result", "agent.query_agent.answer_question"], "gold_not_passed_to_business_code": True, "answer_error_count": len(answer_errors), "execution_error_count": len(execution_errors), "answer_error_types": dict(Counter(str(e.get("type")) for e in answer_errors)), "stage0_contract": "selected_context_refs, selected_tool_refs, and executed_tools remain null/unavailable unless production explicitly exposes them; the evaluator does not infer them from answer text, Gold, route diagnostics, or extra lookups.", "notes": ["Raw channel capture is diagnostic and does not control the answer path.", "Access filtering is applied before RRF fusion.", "Answer availability, successful-call quality and end-to-end quality are reported separately.", "Stage 0 separates stale, irrelevant, access-control, ambiguous and must-not-return diagnostics.", "The current production memory query marks access metadata; read-only checks exclude access_count/last_accessed_at."]}
    for name, payload in [("layer3_run_manifest.json", manifest), ("layer3_metrics.json", metrics), ("layer3_retrieval_metrics.json", retrieval), ("layer3_answer_metrics.json", answer), ("layer3_answer_availability.json", answer_availability), ("layer3_answer_quality.json", answer_quality), ("layer3_citation_metrics.json", citation), ("layer3_no_answer_report.json", no_answer), ("layer3_access_control_report.json", access), ("layer3_route_confusion.json", route_report), ("layer3_tag_breakdown.json", metrics["by_coverage_tag"]), ("layer3_latency_report.json", latency)]:
        (out_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "layer3_predictions.jsonl").open("w", encoding="utf-8") as fh:
        for p in predictions:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    with (out_dir / "layer3_failed_cases.jsonl").open("w", encoding="utf-8") as fh:
        for p in failures:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    lines = ["# Layer 3 检索与回答评测报告", "", f"- Run ID: `{args.run_id}`", f"- Cases: **{len(predictions)}**", f"- Backend: `{os.getenv('STORAGE_BACKEND')}`", f"- Retrieval mode: `{os.getenv('SUIXINJI_MEMORY_RETRIEVAL_MODE')}`", f"- Production entry: `memory_search` + `answer_question`", f"- Answer calls with errors: **{len(answer_errors)}**", f"- Execution/seed errors: **{len(execution_errors)}**", f"- Answer error types: `{dict(Counter(str(e.get('type')) for e in answer_errors))}`", "", "## 总体指标", "", "```json", json.dumps(metrics["overall"], ensure_ascii=False, indent=2), "```", "", "## 按数据集", ""]
    for ds, data in metrics["by_dataset"].items():
        lines += [f"### {ds}", "", "```json", json.dumps(data, ensure_ascii=False, indent=2), "```", ""]
    lines += ["## Stage 2 Semantic Retrieval 诊断", "", "```json", json.dumps(metrics.get("stage2_semantic_retrieval", {}), ensure_ascii=False, indent=2), "```", ""]
    lines += ["## 关键安全与一致性门禁", "", f"- 敏感/越权访问：`{access['access_violation_count']}`", f"- 跨空间返回：`{access['cross_space_violation_count']}`", f"- 业务状态变更：`{access['business_state_mutation_count']}`（访问计数和最后访问时间不计入）", f"- restricted 期望/预测：`{access['restricted_expected_count']}` / `{access['restricted_predicted_count']}`", f"- stale 检索违规率：`{metrics['overall']['stale_retrieval_violation_rate']}`", f"- must-not-return 违规率：`{metrics['overall']['must_not_return_violation_rate']}`", f"- irrelevant 检索返回率：`{metrics['overall']['irrelevant_retrieval_rate']}`", f"- ambiguous candidate 使用率：`{metrics['overall']['ambiguous_candidate_usage_rate']}`", f"- stale 内容进入答案率：`{metrics['overall']['stale_answer_usage_rate']}`", f"- 禁止性断言率：`{metrics['overall']['forbidden_claim_rate']}`", "", "## 延迟", "", "```json", json.dumps(latency, ensure_ascii=False, indent=2), "```", "", "## Stage 0/2 说明", "", "- `selected_context_refs`、`selected_tool_refs`、`executed_tools` 在当前生产入口未暴露统一契约时保持 `null/unavailable`；评测器不会根据答案文本、Gold、路由诊断或额外补查推断这些字段。", "- `raw_channel_hits` 保存 exact/structured/FTS/trigram/vector 的原始命中与 RRF/策略分数；它只用于诊断，不改变生产答案路径。", "- Stage 2 会对 isolated seed memories 使用真实 memory vector lifecycle 补齐 ready `memory_vectors`，并记录 query embedding 状态、embedding contract 与 raw vector similarity。", "- 历史版本、敏感访问上下文、pending-review 在当前生产入口中的支持程度会通过指标暴露，不在评测脚本中补写业务逻辑。", "- 每条 case 使用独立空间，测试结束后删除空间及其级联数据。"]
    (out_dir / "layer3_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", default=f"layer3_{time.strftime('%Y%m%d_%H%M%S')}")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="test only the first N cases (smoke test)")
    args = parser.parse_args()
    cases = load_cases(args.data_dir)
    if args.limit > 0:
        cases = cases[: args.limit]
    started = now_iso()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    predictions: list[dict[str, Any]] = []
    lock = threading.Lock()
    checkpoint = out_dir / "layer3_predictions.inprogress.jsonl"

    def execute(case: dict[str, Any]) -> dict[str, Any]:
        runner = CaseRunner(case, args.run_id, args.top_k)
        try:
            return runner.run()
        except Exception as exc:
            return {"case_id": case.get("case_id"), "dataset": case.get("dataset"), "errors": [{"stage": "runner", "type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}]}
        finally:
            try:
                runner.cleanup()
            except Exception:
                pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(execute, case) for case in cases]
        for idx, future in enumerate(concurrent.futures.as_completed(futures), 1):
            pred = future.result()
            predictions.append(pred)
            with lock, checkpoint.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(pred, ensure_ascii=False) + "\n")
            if idx % 10 == 0 or idx == len(futures):
                print(f"progress {idx}/{len(futures)}", flush=True)
    predictions.sort(key=lambda x: str(x.get("case_id")))
    scored = [score_case(p) for p in predictions]
    write_reports(out_dir, predictions, scored, started, args)
    print(json.dumps({"run_id": args.run_id, "cases": len(predictions), "output_dir": str(out_dir)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
