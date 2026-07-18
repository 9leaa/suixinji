"""PostgreSQL note repository."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import Note, NoteRelation, NoteTag
from core.sensitive import contains_sensitive_data
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_note(row: Note, tags: list[str], related: list[str]) -> dict[str, Any]:
    metadata = dict(row.metadata_json or {})
    return {
        "id": row.id,
        "message_id": row.message_id,
        "space_id": row.space_id,
        "tenant_id": row.tenant_id,
        "ts": row.created_at.isoformat(),
        "title": row.title,
        "tags": tags,
        "type": row.note_type,
        "summary": row.summary,
        "text": row.text,
        "related": related,
        "enrichment_status": row.enrichment_status,
        "enrichment_attempts": row.enrichment_attempts,
        "enrichment_error": row.enrichment_error,
        "enrichment_started_at": _iso(row.enrichment_started_at),
        "enrichment_updated_at": _iso(row.enrichment_updated_at),
        "sensitivity": row.sensitivity,
        **metadata,
    }


def _load_parts(session: Any, note_ids: list[str]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    tags: dict[str, list[str]] = {note_id: [] for note_id in note_ids}
    related: dict[str, list[str]] = {note_id: [] for note_id in note_ids}
    if not note_ids:
        return tags, related
    for note_id, tag in session.execute(select(NoteTag.note_id, NoteTag.tag).where(NoteTag.note_id.in_(note_ids))):
        tags[str(note_id)].append(str(tag))
    for note_id, target_id in session.execute(
        select(NoteRelation.source_note_id, NoteRelation.target_note_id).where(NoteRelation.source_note_id.in_(note_ids))
    ):
        related[str(note_id)].append(str(target_id))
    return tags, related


def save_note(meta: Any) -> bool:
    values = asdict(meta) if not isinstance(meta, dict) else dict(meta)
    space_id = str(values["space_id"])
    tenant_id = str(values.get("tenant_id") or DEFAULT_TENANT_ID)
    standard_keys = {
        "id", "message_id", "space_id", "ts", "title", "tags", "type", "summary", "text", "related",
        "enrichment_status", "enrichment_attempts", "enrichment_error", "enrichment_started_at",
        "enrichment_updated_at", "sensitivity", "tenant_id",
    }
    metadata = {key: value for key, value in values.items() if key not in standard_keys}
    with session_scope() as session:
        space_id = ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        note_id = session.execute(
            insert(Note)
            .values(
                id=str(values["id"]),
                message_id=str(values["message_id"]),
                tenant_id=tenant_id,
                space_id=space_id,
                created_at=parse_datetime(values.get("ts")),
                title=str(values.get("title") or ""),
                note_type=str(values.get("type") or "other"),
                summary=str(values.get("summary") or ""),
                text=str(values.get("text") or ""),
                metadata_json=metadata,
                enrichment_status=str(values.get("enrichment_status") or "ready"),
                enrichment_attempts=int(values.get("enrichment_attempts") or 0),
                enrichment_error=values.get("enrichment_error"),
                enrichment_started_at=parse_datetime(values["enrichment_started_at"]) if values.get("enrichment_started_at") else None,
                enrichment_updated_at=parse_datetime(values["enrichment_updated_at"]) if values.get("enrichment_updated_at") else None,
                sensitivity=str(values.get("sensitivity") or "normal"),
            )
            .on_conflict_do_nothing(constraint="uq_notes_space_message")
            .returning(Note.id)
        ).scalar_one_or_none()
        if note_id is None:
            return False
        for tag in values.get("tags") or []:
            session.execute(insert(NoteTag).values(note_id=note_id, tag=str(tag)).on_conflict_do_nothing())
        for related_id in values.get("related") or []:
            session.execute(
                insert(NoteRelation)
                .values(source_note_id=note_id, target_note_id=str(related_id), relation="related")
                .on_conflict_do_nothing()
            )
        return True


def load_index(space_id: str) -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = list(session.execute(select(Note).where(Note.space_id == space_id).order_by(Note.created_at, Note.id)).scalars())
        tags, related = _load_parts(session, [row.id for row in rows])
        return [_as_note(row, tags[row.id], related[row.id]) for row in rows]


def _query_notes(
    space_id: str,
    *,
    note_type: str | None = None,
    tags: list[str] | None = None,
    match_all_tags: bool = True,
    created_after: datetime | None = None,
    enrichment_statuses: tuple[str, ...] | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    requested_tags = sorted(set(tags or []))
    statement = select(Note).where(Note.space_id == space_id, Note.sensitivity == "normal")
    if note_type:
        statement = statement.where(Note.note_type == note_type)
    if created_after is not None:
        statement = statement.where(Note.created_at >= created_after)
    if enrichment_statuses:
        statement = statement.where(Note.enrichment_status.in_(enrichment_statuses))
    if requested_tags:
        tag_ids = select(NoteTag.note_id).where(NoteTag.tag.in_(requested_tags)).group_by(NoteTag.note_id)
        if match_all_tags:
            tag_ids = tag_ids.having(func.count(func.distinct(NoteTag.tag)) == len(requested_tags))
        statement = statement.where(Note.id.in_(tag_ids))
    bounded_limit = max(1, min(int(limit), 500))
    statement = statement.order_by(Note.created_at.desc(), Note.id.desc()).limit(bounded_limit * 4)
    with session_scope() as session:
        rows = list(session.execute(statement).scalars())
        tags_by_id, related = _load_parts(session, [row.id for row in rows])
        notes = [_as_note(row, tags_by_id[row.id], related[row.id]) for row in rows]
        return [note for note in notes if not contains_sensitive_data(str(note.get("text") or ""))][:bounded_limit]


def query_notes_by_type(space_id: str, note_type: str, *, limit: int = 30) -> list[dict[str, Any]]:
    return _query_notes(space_id, note_type=note_type, limit=limit)


def query_notes_by_tags(
    space_id: str,
    tags: list[str],
    *,
    note_type: str | None = None,
    match_all_tags: bool = True,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return _query_notes(
        space_id,
        note_type=note_type,
        tags=tags,
        match_all_tags=match_all_tags,
        limit=limit,
    )


def list_recent_notes(space_id: str, *, created_after: datetime, limit: int = 30) -> list[dict[str, Any]]:
    return _query_notes(space_id, created_after=created_after, limit=limit)


def list_provisional_notes(space_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    return _query_notes(
        space_id,
        enrichment_statuses=("provisional", "enriching", "failed"),
        limit=limit,
    )


def get_note_relations(space_id: str, note_id: str, *, limit: int = 5) -> dict[str, Any] | None:
    bounded_limit = max(1, min(int(limit), 20))
    with session_scope() as session:
        source = session.execute(
            select(Note).where(Note.space_id == space_id, Note.id == note_id, Note.sensitivity == "normal")
        ).scalar_one_or_none()
        if source is None or contains_sensitive_data(source.text):
            return None
        relation_rows = list(
            session.execute(
                select(NoteRelation.source_note_id, NoteRelation.target_note_id).where(
                    or_(NoteRelation.source_note_id == note_id, NoteRelation.target_note_id == note_id)
                )
            )
        )
        outbound_ids = [str(target) for source_id, target in relation_rows if str(source_id) == note_id][:bounded_limit]
        inbound_ids = [str(source_id) for source_id, target in relation_rows if str(target) == note_id][:bounded_limit]
        related_ids = list(dict.fromkeys([*outbound_ids, *inbound_ids]))
        related_rows = list(
            session.execute(
                select(Note).where(
                    Note.space_id == space_id,
                    Note.id.in_(related_ids),
                    Note.sensitivity == "normal",
                )
            ).scalars()
        ) if related_ids else []
        all_rows = [source, *related_rows]
        tags_by_id, _related = _load_parts(session, [row.id for row in all_rows])
        notes = {
            row.id: _as_note(row, tags_by_id[row.id], outbound_ids if row.id == note_id else [])
            for row in all_rows
            if not contains_sensitive_data(row.text)
        }
        return {
            "source": notes.get(note_id),
            "outbound": [notes[item_id] for item_id in outbound_ids if item_id in notes],
            "inbound": [notes[item_id] for item_id in inbound_ids if item_id in notes],
        }


def list_space_ids() -> list[str]:
    with session_scope() as session:
        return list(session.execute(select(Note.space_id).distinct().order_by(Note.space_id)).scalars())


def find_note(space_id: str, note_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        row = session.execute(select(Note).where(Note.space_id == space_id, Note.id == note_id)).scalar_one_or_none()
        if row is None:
            return None
        tags, related = _load_parts(session, [row.id])
        return _as_note(row, tags[row.id], related[row.id])


def find_note_content(space_id: str, note_id: str) -> dict[str, Any] | None:
    """Load only fields needed by memory extraction in one SQL query."""
    with session_scope() as session:
        tags = (
            select(func.array_agg(NoteTag.tag))
            .where(NoteTag.note_id == Note.id)
            .correlate(Note)
            .scalar_subquery()
        )
        row = session.execute(
            select(
                Note.id,
                Note.message_id,
                Note.space_id,
                Note.tenant_id,
                Note.created_at,
                Note.title,
                Note.note_type,
                Note.summary,
                Note.text,
                Note.sensitivity,
                tags.label("tags"),
            ).where(Note.space_id == space_id, Note.id == note_id)
        ).one_or_none()
        if row is None:
            return None
        return {
            "id": str(row.id),
            "message_id": str(row.message_id),
            "space_id": str(row.space_id),
            "tenant_id": str(row.tenant_id),
            "ts": row.created_at.isoformat(),
            "title": str(row.title),
            "tags": list(row.tags or []),
            "type": str(row.note_type),
            "summary": str(row.summary),
            "text": str(row.text),
            "sensitivity": str(row.sensitivity),
        }


def note_exists(space_id: str, message_id: str) -> bool:
    with session_scope() as session:
        return session.execute(select(Note.id).where(Note.space_id == space_id, Note.message_id == message_id).limit(1)).scalar_one_or_none() is not None


def update_note_metadata(space_id: str, note_id: str, **updates: Any) -> dict[str, Any] | None:
    column_map = {
        "title": "title", "type": "note_type", "summary": "summary", "text": "text",
        "enrichment_status": "enrichment_status", "enrichment_attempts": "enrichment_attempts",
        "enrichment_error": "enrichment_error", "sensitivity": "sensitivity",
    }
    values = {column_map[key]: value for key, value in updates.items() if key in column_map}
    for key in ("enrichment_started_at", "enrichment_updated_at"):
        if key in updates:
            values[key] = parse_datetime(updates[key]) if updates[key] else None
    with session_scope() as session:
        if values:
            session.execute(update(Note).where(Note.space_id == space_id, Note.id == note_id).values(**values))
        if "tags" in updates:
            session.execute(delete(NoteTag).where(NoteTag.note_id == note_id))
            for tag in updates["tags"] or []:
                session.add(NoteTag(note_id=note_id, tag=str(tag)))
        if "related" in updates:
            session.execute(delete(NoteRelation).where(NoteRelation.source_note_id == note_id))
            for target in updates["related"] or []:
                session.add(NoteRelation(source_note_id=note_id, target_note_id=str(target), relation="related"))
    return find_note(space_id, note_id)


def list_pending_enrichments(*, limit: int = 100, max_attempts: int = 3) -> list[dict[str, str]]:
    with session_scope() as session:
        rows = session.execute(
            select(Note.space_id, Note.id)
            .where(
                Note.enrichment_status.in_(["provisional", "enriching", "failed"]),
                Note.enrichment_attempts < max_attempts,
                Note.sensitivity == "normal",
            )
            .order_by(Note.created_at)
            .limit(max(1, int(limit)))
        )
        return [{"space_id": str(space_id), "note_id": str(note_id)} for space_id, note_id in rows]
