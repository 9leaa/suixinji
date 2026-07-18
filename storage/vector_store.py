"""Local JSON vector store for the P2 RAG stage."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.file_lock import locked_space
from core.sensitive import contains_sensitive_data
from storage.note_storage import note_dir


@dataclass
class VectorItem:
    """表示一条已入库笔记的向量记录。

    功能说明:
        保存笔记 ID、平台消息 ID、原文、embedding 向量和轻量元数据，供后续语义检索使用。

    传参说明:
        note_id: 本系统中的笔记 ID，通常对应 NoteMetadata.id。
        message_id: 平台消息 ID，用于幂等去重。
        text: 参与语义检索的原始文本。
        embedding: 文本对应的向量，当前项目中通常是 1024 维。
        metadata: 额外元数据，例如 title、tags、type、summary。

    返回类型说明:
        VectorItem: 一条可写入本地向量索引的记录实例。
    """

    note_id: str
    message_id: str
    text: str
    embedding: list[float]
    metadata: dict[str, Any]


@dataclass
class SearchResult:
    """表示一次语义检索返回的相关笔记结果。

    功能说明:
        保存命中的笔记信息和相似度分数，供 worker 写入 related 字段或后续查询 agent 使用。

    传参说明:
        note_id: 命中的笔记 ID。
        message_id: 命中的平台消息 ID。
        score: 与查询向量的余弦相似度分数。
        text: 命中笔记的原始文本。
        metadata: 命中笔记的轻量元数据。

    返回类型说明:
        SearchResult: 一条语义检索结果实例。
    """

    note_id: str
    message_id: str
    score: float
    text: str
    metadata: dict[str, Any]


def vector_index_path(space_id: str) -> Path:
    """获取指定 space_id 的本地向量索引文件路径。

    功能说明:
        在 `data/notes/{space_id}/vectors/` 下维护一个 `index.json`，用于保存该空间的向量记录。
        如果目录不存在，会自动创建。

    传参说明:
        space_id: 会话/用户隔离 ID。

    返回类型说明:
        Path: 当前 space_id 对应的向量索引 JSON 文件路径。
    """
    path = note_dir(space_id) / "vectors"
    path.mkdir(parents=True, exist_ok=True)
    return path / "index.json"


def load_vector_items(space_id: str) -> list[VectorItem]:
    """读取指定 space_id 的全部向量记录。

    功能说明:
        从本地 `vectors/index.json` 中读取向量记录，并转换为 VectorItem 实例列表。
        如果索引文件不存在，则返回空列表。

    传参说明:
        space_id: 会话/用户隔离 ID。

    返回类型说明:
        list[VectorItem]: 当前空间下已保存的向量记录列表。
    """
    path = vector_index_path(space_id)
    with locked_space(space_id):
        if not path.exists():
            return []

        with path.open("r", encoding="utf-8") as f:
            raw_items = json.load(f)

        return [VectorItem(**item) for item in raw_items]


def save_vector_items(space_id: str, items: list[VectorItem]) -> None:
    """保存指定 space_id 的全部向量记录。

    功能说明:
        将 VectorItem 列表序列化为 JSON，并覆盖写入本地向量索引文件。

    传参说明:
        space_id: 会话/用户隔离 ID。
        items: 需要保存的向量记录列表。

    返回类型说明:
        None: 该函数只执行文件写入，不返回业务结果。
    """
    path = vector_index_path(space_id)
    with locked_space(space_id):
        with path.open("w", encoding="utf-8") as f:
            json.dump([asdict(item) for item in items], f, ensure_ascii=False, indent=2)


def vector_item_exists(space_id: str, note_id: str, message_id: str | None = None) -> bool:
    """判断某条笔记是否已经写入向量索引。

    功能说明:
        根据 note_id 或 message_id 检查本地向量索引中是否已有同一笔记，避免重复写入向量。

    传参说明:
        space_id: 会话/用户隔离 ID。
        note_id: 本系统中的笔记 ID。
        message_id: 平台消息 ID，可为空；传入时也参与判断。

    返回类型说明:
        bool: 如果向量索引中已存在该笔记，返回 True；否则返回 False。
    """
    for item in load_vector_items(space_id):
        if item.note_id == note_id:
            return True
        if message_id is not None and item.message_id == message_id:
            return True
    return False


def add_vector_item(space_id: str, item: VectorItem) -> bool:
    """向本地向量索引中追加一条记录。

    功能说明:
        以 note_id 和 message_id 做幂等判断。若记录已存在，则跳过写入；否则追加并保存。

    传参说明:
        space_id: 会话/用户隔离 ID。
        item: 需要写入的向量记录。

    返回类型说明:
        bool: 成功新增时返回 True；检测到重复并跳过时返回 False。
    """
    with locked_space(space_id):
        items = load_vector_items(space_id)
        for existing in items:
            if existing.note_id == item.note_id or existing.message_id == item.message_id:
                return False

        items.append(item)
        save_vector_items(space_id, items)
        return True


def remove_vector_item(space_id: str, note_id: str) -> bool:
    """Remove one vector record, primarily for privacy cleanup."""
    with locked_space(space_id):
        items = load_vector_items(space_id)
        kept = [item for item in items if item.note_id != note_id]
        if len(kept) == len(items):
            return False
        save_vector_items(space_id, kept)
        return True


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量之间的余弦相似度。

    功能说明:
        使用 dot(a, b) / (||a|| * ||b||) 衡量两个 embedding 的语义相似度。
        分数越接近 1 越相似，越接近 0 越不相关。

    传参说明:
        a: 第一个向量。
        b: 第二个向量。

    返回类型说明:
        float: 两个向量的余弦相似度分数。
    """
    if len(a) != len(b):
        raise ValueError(f"Embedding dimensions differ: {len(a)} != {len(b)}")

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot / (norm_a * norm_b)


def search_related(
    space_id: str,
    query_embedding: list[float],
    *,
    top_k: int = 3,
    exclude_note_id: str | None = None,
    min_score: float | None = None,
) -> list[SearchResult]:
    """在历史向量中检索与查询向量最相关的笔记。

    功能说明:
        遍历当前 space_id 下已有的全部向量记录，计算余弦相似度，按分数降序返回 top_k 条。
        可选排除当前笔记 ID，避免新笔记和自身建立 related 关系。

    传参说明:
        space_id: 会话/用户隔离 ID。
        query_embedding: 查询文本对应的 embedding 向量。
        top_k: 返回的最大结果数，默认 3。
        exclude_note_id: 需要排除的笔记 ID，可为空。
        min_score: 最低相似度阈值，可为空；为空时不按阈值过滤。

    返回类型说明:
        list[SearchResult]: 按相似度从高到低排列的相关笔记列表。
    """
    if top_k <= 0:
        return []

    results: list[SearchResult] = []
    for item in load_vector_items(space_id):
        if exclude_note_id is not None and item.note_id == exclude_note_id:
            continue
        sensitivity = str(item.metadata.get("sensitivity") or "normal").casefold()
        if sensitivity not in {"", "normal", "none"} or contains_sensitive_data(item.text):
            continue

        score = cosine_similarity(query_embedding, item.embedding)
        if min_score is not None and score < min_score:
            continue

        results.append(
            SearchResult(
                note_id=item.note_id,
                message_id=item.message_id,
                score=score,
                text=item.text,
                metadata=item.metadata,
            )
        )

    results.sort(key=lambda result: result.score, reverse=True)
    return results[:top_k]


def search_related_note_ids(
    space_id: str,
    query_embedding: list[float],
    *,
    top_k: int = 3,
    exclude_note_id: str | None = None,
    min_score: float | None = None,
) -> list[str]:
    """检索相关笔记并只返回 note_id 列表。

    功能说明:
        对 search_related 的轻量封装，适合直接填充 NoteMetadata.related 字段。

    传参说明:
        space_id: 会话/用户隔离 ID。
        query_embedding: 查询文本对应的 embedding 向量。
        top_k: 返回的最大结果数，默认 3。
        exclude_note_id: 需要排除的笔记 ID，可为空。
        min_score: 最低相似度阈值，可为空。

    返回类型说明:
        list[str]: 相关笔记 ID 列表。
    """
    return [
        result.note_id
        for result in search_related(
            space_id,
            query_embedding,
            top_k=top_k,
            exclude_note_id=exclude_note_id,
            min_score=min_score,
        )
    ]


from core.settings import STORAGE_BACKEND as _STORAGE_BACKEND

if _STORAGE_BACKEND == "postgres":
    from repositories.postgres import vectors as _postgres_vectors

    load_vector_items = _postgres_vectors.load_vector_items
    save_vector_items = _postgres_vectors.save_vector_items
    vector_item_exists = _postgres_vectors.vector_item_exists
    add_vector_item = _postgres_vectors.add_vector_item
    remove_vector_item = _postgres_vectors.remove_vector_item
    search_related = _postgres_vectors.search_related
    search_related_note_ids = _postgres_vectors.search_related_note_ids
