"""文件作用：Note 向量数据访问。

项目关系：本文件依赖 `core.config`、`core.sensitive`、`infrastructure.database`、`infrastructure.schema` 等 5 个模块；被 `eval.large_live_retrieval_eval`、`eval.live_retrieval_eval`。
"""



from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from core.config import get_embedding_config
from core.sensitive import contains_sensitive_data
from infrastructure.database import session_scope
from infrastructure.schema import Note, NoteEmbedding


def _vector_item(row: NoteEmbedding) -> Any:
    """函数功能：`_vector_item` 负责处理 vector item，服务于本文件职责：Note 向量数据访问。
    传参：
        row: row 参数，由调用方传入，类型为 `NoteEmbedding`。
    返回结果说明：
        返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    from storage.vector_store import VectorItem
    metadata = dict(row.metadata_json or {})
    return VectorItem(
        note_id=row.note_id,
        message_id=str(metadata.get("message_id") or ""),
        text=row.text,
        embedding=[float(value) for value in row.embedding],
        metadata=metadata,
    )


def load_vector_items(space_id: str) -> list[Any]:
    """函数功能：`load_vector_items` 负责加载 vector items，服务于本文件职责：Note 向量数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[Any]`，表示按条件筛选、构造或查询得到的列表。
    """
    with session_scope() as session:
        rows = session.execute(
            select(NoteEmbedding)
            .join(Note, Note.id == NoteEmbedding.note_id)
            .where(Note.space_id == space_id)
            .order_by(Note.created_at)
        ).scalars()
        return [_vector_item(row) for row in rows]


def save_vector_items(space_id: str, items: list[Any]) -> None:
    """函数功能：`save_vector_items` 负责保存 vector items，服务于本文件职责：Note 向量数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        items: 待遍历或处理的元素集合，类型为 `list[Any]`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    existing = {item.note_id for item in load_vector_items(space_id)}
    incoming = {item.note_id for item in items}
    for note_id in existing - incoming:
        remove_vector_item(space_id, note_id)
    for item in items:
        add_vector_item(space_id, item)


def vector_item_exists(space_id: str, note_id: str, message_id: str | None = None) -> bool:
    """函数功能：`vector_item_exists` 负责处理 vector item exists，服务于本文件职责：Note 向量数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    with session_scope() as session:
        statement = select(NoteEmbedding.note_id).join(Note).where(Note.space_id == space_id)
        if note_id:
            statement = statement.where(NoteEmbedding.note_id == note_id)
        elif message_id:
            statement = statement.where(Note.message_id == message_id)
        return session.execute(statement.limit(1)).scalar_one_or_none() is not None


def add_vector_item(space_id: str, item: Any) -> bool:
    """函数功能：`add_vector_item` 负责处理 add vector item，服务于本文件职责：Note 向量数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        item: item 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if len(item.embedding) != 1024:
        raise ValueError(f"PostgreSQL note embedding must have 1024 dimensions, got {len(item.embedding)}")
    model = str(item.metadata.get("embedding_model") or get_embedding_config().model)
    metadata = dict(item.metadata)
    metadata.setdefault("message_id", item.message_id)
    with session_scope() as session:
        note_space = session.execute(select(Note.space_id).where(Note.id == item.note_id)).scalar_one_or_none()
        if note_space is None:
            raise ValueError(f"note does not exist: {item.note_id}")
        if str(note_space) != space_id:
            raise ValueError("note belongs to a different space")
        created = session.execute(
            insert(NoteEmbedding)
            .values(
                note_id=item.note_id,
                model=model,
                dimensions=len(item.embedding),
                embedding=item.embedding,
                text=item.text,
                metadata_json=metadata,
            )
            .on_conflict_do_nothing(index_elements=[NoteEmbedding.note_id, NoteEmbedding.model])
            .returning(NoteEmbedding.note_id)
        ).scalar_one_or_none()
        return created is not None


def remove_vector_item(space_id: str, note_id: str) -> bool:
    """函数功能：`remove_vector_item` 负责移除 vector item，服务于本文件职责：Note 向量数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    with session_scope() as session:
        result = session.execute(
            delete(NoteEmbedding)
            .where(NoteEmbedding.note_id == note_id, NoteEmbedding.note_id.in_(select(Note.id).where(Note.space_id == space_id)))
            .returning(NoteEmbedding.note_id)
        ).first()
        return result is not None


def search_related(
    space_id: str,
    query_embedding: list[float],
    *,
    top_k: int = 3,
    exclude_note_id: str | None = None,
    min_score: float | None = None,
) -> list[Any]:
    """函数功能：`search_related` 负责搜索 related，服务于本文件职责：Note 向量数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query_embedding: query embedding 参数，由调用方传入，类型为 `list[float]`。
        top_k: top k 参数，由调用方传入，类型为 `int`，默认值为 `3`。
        exclude_note_id: exclude note id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        min_score: min score 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[Any]`，表示按条件筛选、构造或查询得到的列表。
    """
    from storage.vector_store import SearchResult
    if top_k <= 0:
        return []
    if len(query_embedding) != 1024:
        raise ValueError(f"PostgreSQL query embedding must have 1024 dimensions, got {len(query_embedding)}")
    distance = NoteEmbedding.embedding.cosine_distance(query_embedding)
    statement = (
        select(NoteEmbedding, Note.message_id, (1 - distance).label("score"))
        .join(Note, Note.id == NoteEmbedding.note_id)
        .where(Note.space_id == space_id, Note.sensitivity == "normal")
        .order_by(distance)
        .limit(max(1, int(top_k)))
    )
    if exclude_note_id:
        statement = statement.where(NoteEmbedding.note_id != exclude_note_id)
    if min_score is not None:
        statement = statement.where((1 - distance) >= min_score)
    with session_scope() as session:
        rows = session.execute(statement)
        results = []
        for embedding, message_id, score in rows:
            metadata = dict(embedding.metadata_json or {})
            if contains_sensitive_data(embedding.text):
                continue
            results.append(SearchResult(embedding.note_id, str(message_id), float(score), embedding.text, metadata))
        return results


def search_related_note_ids(space_id: str, query_embedding: list[float], **kwargs: Any) -> list[str]:
    """函数功能：`search_related_note_ids` 负责搜索 related note ids，服务于本文件职责：Note 向量数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query_embedding: query embedding 参数，由调用方传入，类型为 `list[float]`。
        **kwargs: kwargs 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [result.note_id for result in search_related(space_id, query_embedding, **kwargs)]
