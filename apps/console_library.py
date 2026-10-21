"""Read-only four-type memory library and recorded version comparisons."""
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from apps.console import authorize
from apps.fact_observations import protect_history
from infrastructure.database import session_scope
from infrastructure.schema import Space, Note, Memory, MemorySource
from memory import repository, fact_memory as fm
from core import settings

router = APIRouter()


def check_space(space, tenant):
    with session_scope() as session:
        if session.scalar(select(Space.id).where(Space.id == space, Space.tenant_id == tenant)) is None:
            raise HTTPException(404, 'space not found')


def version_pairs(versions):
    ordered = sorted(versions, key=lambda v: v.get('version', 0))
    return [{'version': row.get('version'), 'before': ordered[i - 1] if i else None,
             'after': row, 'reason': row.get('reason') or '未记录修改原因',
             'before_label': '上一记录版本' if i else '没有更早版本记录'}
            for i, row in enumerate(ordered)]


def library_rows(space):
    from datetime import datetime, timezone
    with session_scope() as session:
        records = session.scalars(select(Memory).where(
            Memory.space_id == space, Memory.status == 'active',
            (Memory.valid_until.is_(None)) | (Memory.valid_until > datetime.now(timezone.utc)))).all()
        sources = {}
        for mid, nid in session.execute(select(MemorySource.memory_id, MemorySource.note_id)
                .join(Memory, Memory.id == MemorySource.memory_id).where(Memory.space_id == space)):
            sources.setdefault(mid, []).append(nid)
        rows = [{'id': m.id, 'content': m.content, 'memory_type': m.memory_type,
                 'status': m.status, 'task_status': m.task_status, 'current_version': m.current_version,
                 'updated_at': m.updated_at.isoformat(), 'source_note_ids': sources.get(m.id, []),
                 'engine': 'legacy'} for m in records]
    if settings.FACT_MEMORY_ENABLED:
        rows += [dict(c, engine='fact') for c in fm.cards(space)]
    return rows


@router.get('/api/console/memories')
def memories(space: str, kind: str = '', q: str = '', offset: int = Query(0, ge=0),
             limit: int = Query(30, ge=1, le=100), tenant=Depends(authorize)):
    check_space(space, tenant)
    rows = library_rows(space)
    counts = {k: sum(r['memory_type'] == k for r in rows) for k in ('preference', 'task', 'semantic', 'episodic')}
    rows = [r for r in rows if (not kind or r['memory_type'] == kind)
            and (not q or q.casefold() in r.get('content', '').casefold())]
    rows.sort(key=lambda r: str(r.get('updated_at') or r.get('created_at') or ''), reverse=True)
    visible = rows[offset:offset + limit]
    if settings.FACT_MEMORY_ENABLED:
        visible = protect_history(space, visible)
    return {'items': visible, 'counts': counts, 'total': len(rows), 'offset': offset,
            'limit': limit, 'space_id': space, 'scope': '当前有效记忆；历史变化以版本快照为准'}


@router.get('/api/console/memory-history')
def memory_history(space: str, ident: str, engine: str, tenant=Depends(authorize)):
    check_space(space, tenant)
    if engine == 'fact' and settings.FACT_MEMORY_ENABLED:
        card = fm.get(space, 'card', ident)
        if not card or card.get('status') != 'active':
            raise HTTPException(404, 'memory not found')
        versions = [v for v in fm.items(space, 'version') if v['id'] == ident]
        source_ids = {f['note_id'] for f in fm.items(space, 'fact') if f['id'] in card.get('source_fact_ids', [])}
    elif engine == 'legacy':
        record = repository.get_memory(ident)
        if not record or record.space_id != space or record.status != 'active':
            raise HTTPException(404, 'memory not found')
        versions = [asdict(v) for v in record.versions]
        source_ids = {s.note_id for s in record.sources}
    else:
        raise HTTPException(404, 'memory not found')
    with session_scope() as session:
        sources = [{'id': n.id, 'text': n.text} for n in session.scalars(
            select(Note).where(Note.tenant_id == tenant, Note.space_id == space, Note.id.in_(source_ids))).all()]
    if settings.FACT_MEMORY_ENABLED:
        versions = protect_history(space, versions)
        sources = [fm.mask_note(space, n) for n in sources]
    return {'id': ident, 'space_id': space, 'changes': version_pairs(versions), 'sources': sources,
            'source_notice': '下方原文为当前笔记，可能经过编辑；不冒充历史模型输入。'}
