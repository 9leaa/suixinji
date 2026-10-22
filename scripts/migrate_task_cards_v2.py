"""Deterministically materialize existing legacy task projections; no model calls."""
import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--expected-database', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from core.settings import DATABASE_URL
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.engine import make_url
    from repositories.postgres import memory as repo
    from memory.task_cards import project, card_key, family_key, VERSION
    if make_url(DATABASE_URL).database != args.expected_database:
        raise SystemExit('Database identity mismatch')
    url = DATABASE_URL.replace('postgresql://', 'postgresql+psycopg://', 1)
    with Session(create_engine(url)) as session, session.begin():
        rows = session.scalars(select(repo.Memory).where(
            repo.Memory.memory_type == 'task', repo.Memory.memory_key_version != VERSION
        ).order_by(repo.Memory.id).with_for_update()).all()
        backup, plans = [], []
        for row in rows:
            backup.append({c.name: getattr(row, c.name) for c in row.__table__.columns})
            sources = session.scalars(select(repo.MemorySourceRow).where(
                repo.MemorySourceRow.memory_id == row.id)).all()
            old_scope = dict(row.scope_json or {})
            record = SimpleNamespace(id=row.id, memory_key_version=row.memory_key_version,
                scope=old_scope, subject=row.subject, object_value=row.object_value,
                content=row.content, task_status=row.task_status, sources=sources)
            card = project(record)
            if not card['subject'] or not card['task'] or card['task_status'] not in {'todo', 'done'}:
                raise ValueError(f'Cannot migrate task without inventing identity: {row.id}')
            # Preserve historical interpretation; never reinterpret relative dates today.
            owner = old_scope.get('owner_id', 'owner')
            scope = {k: v for k, v in old_scope.items() if k in
                     {'owner_id', 'subject_id', 'access_scope', 'sensitivity'}}
            scope.update(task_card=card, canonical_topic=card['task'],
                         task_family_key=family_key(card['subject'], card['task'], owner))
            plans.append((row, card, scope, card_key(card['subject'], card['task'], card['instance'], owner, row.id)))
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump({'database': args.expected_database, 'rows': backup}, out, ensure_ascii=False, default=str, indent=2)
        if args.apply:
            for row, card, scope, key in plans:
                row.predicate = 'task'
                repo._versioned_update(session, row, scope=scope, object_value=card['task'],
                    memory_key=key, memory_key_version=VERSION,
                    reason='deterministic_task_card_v2_migration', source_note_id=None)
        print(json.dumps({'planned': len(plans), 'migrated': len(plans) if args.apply else 0,
                          'backup': str(args.backup), 'model_calls': 0}))


if __name__ == '__main__':
    main()
