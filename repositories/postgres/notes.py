"""PostgreSQL note repository."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import Note, NoteRelation, NoteTag
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_note(row: Note, tags: list[str], related: list[str]) -> dict[str, Any]:
    metadata = dict(row.metadata_json or {})
    return {
        "id": row.id,
        "message_id": row.message_id,
        "space_id": row.space_id,
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
        ensure_tenant_space(session, space_id, tenant_id=tenant_id)
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
