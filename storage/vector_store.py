"""文件作用：本地 Note 向量检索。

项目关系：本文件依赖 `core.file_lock`、`core.sensitive`、`core.settings`、`repositories.postgres` 等 5 个模块；被 `agent.query_agent`、`core.worker`、`eval.eval_query_react`、`eval.eval_retrieval` 等 8 个模块。
"""



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
    """类功能：`VectorItem` 封装与“本地 Note 向量检索”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """

    note_id: str
    message_id: str
    text: str
    embedding: list[float]
    metadata: dict[str, Any]


@dataclass
class SearchResult:
    """类功能：`SearchResult` 封装与“本地 Note 向量检索”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """

    note_id: str
    message_id: str
    score: float
    text: str
    metadata: dict[str, Any]


def vector_index_path(space_id: str) -> Path:
    """函数功能：`vector_index_path` 负责处理 vector index path，服务于本文件职责：本地 Note 向量检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `Path` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    path = note_dir(space_id) / "vectors"
    path.mkdir(parents=True, exist_ok=True)
    return path / "index.json"


def load_vector_items(space_id: str) -> list[VectorItem]:
    """函数功能：`load_vector_items` 负责加载 vector items，服务于本文件职责：本地 Note 向量检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[VectorItem]`，表示按条件筛选、构造或查询得到的列表。
    """
    path = vector_index_path(space_id)
    with locked_space(space_id):
        if not path.exists():
            return []

        with path.open("r", encoding="utf-8") as f:
            raw_items = json.load(f)

        return [VectorItem(**item) for item in raw_items]


def save_vector_items(space_id: str, items: list[VectorItem]) -> None:
    """函数功能：`save_vector_items` 负责保存 vector items，服务于本文件职责：本地 Note 向量检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        items: 待遍历或处理的元素集合，类型为 `list[VectorItem]`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    path = vector_index_path(space_id)
    with locked_space(space_id):
        with path.open("w", encoding="utf-8") as f:
            json.dump([asdict(item) for item in items], f, ensure_ascii=False, indent=2)


def vector_item_exists(space_id: str, note_id: str, message_id: str | None = None) -> bool:
    """函数功能：`vector_item_exists` 负责处理 vector item exists，服务于本文件职责：本地 Note 向量检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    for item in load_vector_items(space_id):
        if item.note_id == note_id:
            return True
        if message_id is not None and item.message_id == message_id:
            return True
    return False


def add_vector_item(space_id: str, item: VectorItem) -> bool:
    """函数功能：`add_vector_item` 负责处理 add vector item，服务于本文件职责：本地 Note 向量检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        item: item 参数，由调用方传入，类型为 `VectorItem`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
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
    """函数功能：`remove_vector_item` 负责移除 vector item，服务于本文件职责：本地 Note 向量检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    with locked_space(space_id):
        items = load_vector_items(space_id)
        kept = [item for item in items if item.note_id != note_id]
        if len(kept) == len(items):
            return False
        save_vector_items(space_id, kept)
        return True


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """函数功能：`cosine_similarity` 负责处理 cosine similarity，服务于本文件职责：本地 Note 向量检索。
    传参：
        a: a 参数，由调用方传入，类型为 `list[float]`。
        b: b 参数，由调用方传入，类型为 `list[float]`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
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
    """函数功能：`search_related` 负责搜索 related，服务于本文件职责：本地 Note 向量检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query_embedding: query embedding 参数，由调用方传入，类型为 `list[float]`。
        top_k: top k 参数，由调用方传入，类型为 `int`，默认值为 `3`。
        exclude_note_id: exclude note id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        min_score: min score 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[SearchResult]`，表示按条件筛选、构造或查询得到的列表。
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
    """函数功能：`search_related_note_ids` 负责搜索 related note ids，服务于本文件职责：本地 Note 向量检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query_embedding: query embedding 参数，由调用方传入，类型为 `list[float]`。
        top_k: top k 参数，由调用方传入，类型为 `int`，默认值为 `3`。
        exclude_note_id: exclude note id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        min_score: min score 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
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
