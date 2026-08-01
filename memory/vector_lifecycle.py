"""文件作用：Memory 向量生命周期。

项目关系：本文件依赖 `core.config`；被 `repositories.postgres.memory`。
"""



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
    """函数功能：`memory_embedding_text` 负责处理 memory embedding text，服务于本文件职责：Memory 向量生命周期。
    传参：
        memory_type: memory type 参数，由调用方传入，类型为 `str`。
        subject: subject 参数，由调用方传入，类型为 `str | None`。
        predicate: predicate 参数，由调用方传入，类型为 `str | None`。
        object_value: object value 参数，由调用方传入，类型为 `str | None`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
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
    """函数功能：`memory_content_hash` 负责处理 memory content hash，服务于本文件职责：Memory 向量生命周期。
    传参：
        memory_type: memory type 参数，由调用方传入，类型为 `str`。
        subject: subject 参数，由调用方传入，类型为 `str | None`。
        predicate: predicate 参数，由调用方传入，类型为 `str | None`。
        object_value: object value 参数，由调用方传入，类型为 `str | None`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
        model: model 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        dimension: dimension 参数，由调用方传入，类型为 `int | None`，默认值为 `None`。
        embedding_version: embedding version 参数，由调用方传入，类型为 `str`，默认值为 `EMBEDDING_VERSION`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
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
    """函数功能：`current_embedding_contract` 负责处理 current embedding contract，服务于本文件职责：Memory 向量生命周期。
    传参：
        无。
    返回结果说明：
        返回 `tuple[str, int, str]`，表示由多个相关值组成的结果。
    """
    config = get_embedding_config()
    return config.model, int(config.dimension), EMBEDDING_VERSION
