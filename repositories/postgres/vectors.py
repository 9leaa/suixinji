"""PostgreSQL pgvector note embedding repository."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from core.config import get_embedding_config
from core.sensitive import contains_sensitive_data
from infrastructure.database import session_scope
from infrastructure.schema import Note, NoteEmbedding


def _vector_item(row: NoteEmbedding) -> Any:
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
    with session_scope() as session:
        rows = session.execute(
            select(NoteEmbedding)
            .join(Note, Note.id == NoteEmbedding.note_id)
            .where(Note.space_id == space_id)
            .order_by(Note.created_at)
        ).scalars()
        return [_vector_item(row) for row in rows]


def save_vector_items(space_id: str, items: list[Any]) -> None:
    existing = {item.note_id for item in load_vector_items(space_id)}
    incoming = {item.note_id for item in items}
    for note_id in existing - incoming:
        remove_vector_item(space_id, note_id)
    for item in items:
        add_vector_item(space_id, item)


def vector_item_exists(space_id: str, note_id: str, message_id: str | None = None) -> bool:
    with session_scope() as session:
        statement = select(NoteEmbedding.note_id).join(Note).where(Note.space_id == space_id)
        if note_id:
            statement = statement.where(NoteEmbedding.note_id == note_id)
        elif message_id:
            statement = statement.where(Note.message_id == message_id)
        return session.execute(statement.limit(1)).scalar_one_or_none() is not None


def add_vector_item(space_id: str, item: Any) -> bool:
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
    return [result.note_id for result in search_related(space_id, query_embedding, **kwargs)]
