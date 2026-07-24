"""Idempotently enqueue embeddings for active PostgreSQL Memory rows."""

from __future__ import annotations

import argparse
from datetime import datetime

from core.settings import MEMORY_VECTOR_LIFECYCLE_ENABLED
from infrastructure.database import session_scope
from repositories.postgres.dispatch import _enqueue_task_in_session
from repositories.postgres.memory import list_memory_vector_backfill_candidates
from infrastructure.schema import Memory, MemoryVector
from memory.vector_lifecycle import current_embedding_contract


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", default="active")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    candidates = list_memory_vector_backfill_candidates(status=args.status, limit=args.limit)
    report = {"status": args.status, "candidates": len(candidates), "enqueued": 0, "dry_run": not args.execute}
    if args.execute and MEMORY_VECTOR_LIFECYCLE_ENABLED:
        model, dimension, version = current_embedding_contract()
        with session_scope() as session:
            for item in candidates:
                row = session.get(Memory, item["memory_id"])
                if row is None:
                    continue
                vector = session.get(MemoryVector, row.id)
                if vector is None:
                    vector = MemoryVector(
                        memory_id=row.id,
                        model=model,
                        dimension=dimension,
                        content_hash=item["content_hash"],
                        embedding_version=version,
                        status="pending",
                        attempt_count=0,
                        last_error=None,
                        created_at=datetime.now().astimezone(),
                        updated_at=datetime.now().astimezone(),
                    )
                    session.add(vector)
                else:
                    vector.status = "pending"
                    vector.content_hash = item["content_hash"]
                    vector.model = model
                    vector.dimension = dimension
                    vector.embedding_version = version
                    vector.embedding = None
                    vector.next_retry_at = None
                _enqueue_task_in_session(
                    session,
                    task_type="memory_embedding",
                    tenant_id=str(row.tenant_id),
                    space_id=str(row.space_id),
                    source_message_id=None,
                    idempotency_key=f"memory_embedding:{row.id}:{item['content_hash']}",
                    payload={
                        "operation": "memory_embedding",
                        "memory_id": row.id,
                        "content_hash": item["content_hash"],
                        "embedding_version": version,
                    },
                    priority=-1,
                    initial_status="queued",
                    publish=True,
                )
                report["enqueued"] += 1
    print(report)


if __name__ == "__main__":
    main()
