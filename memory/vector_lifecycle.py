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
    """负责“记忆向量文本”。

    该函数是 `memory.vector_lifecycle` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
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
    """负责“记忆contenthash”。

    该函数是 `memory.vector_lifecycle` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
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
    """负责“current向量contract”。

    该函数是 `memory.vector_lifecycle` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    config = get_embedding_config()
    return config.model, int(config.dimension), EMBEDDING_VERSION
