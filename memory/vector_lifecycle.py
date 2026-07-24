"""Shared Memory Vector lifecycle helpers."""

from __future__ import annotations

import hashlib

from core.config import get_embedding_config

EMBEDDING_VERSION = "memory-vector-v1"


def memory_embedding_text(
    *,
    memory_type: str,
    subject: str | None,
    predicate: str | None,
    object_value: str | None,
    content: str,
) -> str:
    return " | ".join(
        [
            str(memory_type or ""),
            str(subject or ""),
            str(predicate or ""),
            str(object_value or ""),
            str(content or ""),
        ]
    )


def memory_content_hash(
    *,
    memory_type: str,
    subject: str | None,
    predicate: str | None,
    object_value: str | None,
    content: str,
    model: str | None = None,
    dimension: int | None = None,
    embedding_version: str = EMBEDDING_VERSION,
) -> str:
    config = get_embedding_config()
    payload = memory_embedding_text(
        memory_type=memory_type,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        content=content,
    )
    metadata = "|".join(
        [
            payload,
            str(model or config.model),
            str(dimension or config.dimension),
            str(embedding_version),
        ]
    )
    return hashlib.sha256(metadata.encode("utf-8")).hexdigest()


def current_embedding_contract() -> tuple[str, int, str]:
    config = get_embedding_config()
    return config.model, int(config.dimension), EMBEDDING_VERSION
