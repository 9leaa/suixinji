"""Evaluation-only server-side reuse of frozen memory embeddings."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any


def complete_seed_memory_vectors_from_bank(
    memory_ids: list[str],
) -> dict[str, Any] | None:
    """Copy frozen vectors inside Postgres instead of retransmitting them per case."""
    if os.getenv("SUIXINJI_EVAL_VECTOR_BANK_ENABLED", "false").lower() != "true":
        return None
    if not memory_ids:
        return {
            "requested": 0,
            "completed": 0,
            "already_ready_or_inactive": 0,
            "failed_count": 0,
            "failed": [],
            "vector_bank_hits": 0,
            "vector_bank_misses": 0,
        }

    from infrastructure.schema import Memory
    from memory.vector_lifecycle import (
        current_embedding_contract,
        memory_content_hash,
        memory_embedding_text,
    )
    from repositories.postgres.memory import session_scope
    from sqlalchemy import select, text

    model, dimension, version = current_embedding_contract()
    with session_scope() as session:
        records = list(
            session.execute(
                select(Memory).where(Memory.id.in_(memory_ids))
            ).scalars()
        )

    rows: list[dict[str, str]] = []
    for record in records:
        embedding_text = memory_embedding_text(
            memory_type=record.memory_type,
            subject=record.subject,
            predicate=record.predicate,
            object_value=record.object_value,
            content=record.content,
        )
        normalized_text = " ".join(embedding_text.split())
        rows.append({
            "memory_id": record.id,
            "cache_hash": hashlib.sha256(
                normalized_text.encode("utf-8")
            ).hexdigest()[:24],
            "content_hash": memory_content_hash(
                memory_type=record.memory_type,
                subject=record.subject,
                predicate=record.predicate,
                object_value=record.object_value,
                content=record.content,
                model=model,
                dimension=int(dimension),
                embedding_version=version,
            ),
        })

    now = datetime.now(timezone.utc)
    statement = text(
        """
        INSERT INTO memory_vectors (
            memory_id, embedding, model, dimension, content_hash,
            embedding_version, status, attempt_count, next_retry_at,
            last_error, created_at, updated_at
        )
        SELECT
            source.memory_id, bank.embedding, :model, :dimension,
            source.content_hash, :embedding_version, 'ready', 1,
            NULL, NULL, :created_at, :updated_at
        FROM jsonb_to_recordset(CAST(:rows AS jsonb))
            AS source(memory_id text, cache_hash text, content_hash text)
        JOIN suixinji_eval_embedding_bank AS bank
          ON bank.cache_hash = source.cache_hash
         AND bank.model = :model
         AND bank.dimension = :dimension
        ON CONFLICT (memory_id) DO UPDATE SET
            embedding = EXCLUDED.embedding,
            model = EXCLUDED.model,
            dimension = EXCLUDED.dimension,
            content_hash = EXCLUDED.content_hash,
            embedding_version = EXCLUDED.embedding_version,
            status = EXCLUDED.status,
            attempt_count = EXCLUDED.attempt_count,
            next_retry_at = EXCLUDED.next_retry_at,
            last_error = EXCLUDED.last_error,
            updated_at = EXCLUDED.updated_at
        RETURNING memory_id
        """
    )
    with session_scope() as session:
        completed_ids = set(
            session.execute(
                statement,
                {
                    "rows": json.dumps(rows, ensure_ascii=False),
                    "model": model,
                    "dimension": int(dimension),
                    "embedding_version": version,
                    "created_at": now,
                    "updated_at": now,
                },
            ).scalars()
        )

    missing_ids = [record.id for record in records if record.id not in completed_ids]
    if missing_ids:
        raise RuntimeError(
            "evaluation vector bank misses frozen embeddings: "
            + ",".join(missing_ids[:10])
        )
    return {
        "requested": len(memory_ids),
        "completed": len(completed_ids),
        "already_ready_or_inactive": 0,
        "failed_count": 0,
        "failed": [],
        "vector_bank_hits": len(completed_ids),
        "vector_bank_misses": 0,
    }
