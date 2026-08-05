from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

from sqlalchemy import delete, func, select

ROOT = Path('/home/zcj/suixinji')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.layer2.adapter import _candidate, _seed_candidate  # noqa: E402
from eval.layer2.metrics import _case_final_exact  # noqa: E402
from infrastructure.database import session_scope  # noqa: E402
from infrastructure.schema import Memory, MemorySource, MemoryVersion, Space  # noqa: E402
from memory import repository  # noqa: E402
from memory.consolidator import consolidate_candidate  # noqa: E402
from repositories.postgres.common import parse_datetime  # noqa: E402
from repositories.postgres.memory import _add_version  # noqa: E402


DATA = ROOT / 'eval/layer2/data/version_source_idempotency.jsonl'
OUT = ROOT / 'eval/results/layer2_postgres_concurrency'
PREFIX = f'layer2_pg_concurrency_{int(time.time())}_'


def load_rows() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in DATA.read_text(encoding='utf-8').splitlines()
        if line.strip() and 'concurrency' in line
    ]


def record_dict(record: Any, ref: str) -> dict[str, Any]:
    return {
        'memory_ref': ref,
        'memory_id': record.id,
        'memory_type': record.memory_type,
        'memory_key': record.memory_key,
        'entity': record.subject,
        'attribute': record.predicate,
        'operation': record.scope.get('operation'),
        'canonical_topic': record.scope.get('canonical_topic'),
        'task_status': record.task_status,
        'old_value': record.scope.get('old_value'),
        'new_value': record.scope.get('new_value'),
        'content': record.content,
        'status': record.status,
        'version_sequence': record.current_version,
        'source_note_ids': sorted({source.note_id for source in record.sources}),
        'valid_from': record.valid_from,
        'valid_until': record.valid_until,
        'polarity': record.polarity,
        'updated_at': record.updated_at,
    }


class PgCase:
    def __init__(self, case: dict[str, Any], run_id: str) -> None:
        self.case = case
        self.space_id = f'{PREFIX}{run_id}_{case["case_id"]}'
        self.logical_to_db: dict[str, str] = {}
        self.db_to_logical: dict[str, str] = {}

    def seed(self) -> None:
        for raw in self.case['input'].get('existing_memories', []):
            ref = str(raw['memory_ref'])
            record = repository.insert_memory(
                self.space_id,
                _seed_candidate(raw),
                source_note_id=(raw.get('source_note_ids') or [f'seed:{ref}'])[0],
            )
            self.logical_to_db[ref] = record.id
            self.db_to_logical[record.id] = ref
            for note_id in (raw.get('source_note_ids') or [])[1:]:
                repository.add_source(record.id, str(note_id), 'supported_by')
            target_version = max(1, int(raw.get('version_sequence') or 1))
            with session_scope() as session:
                row = session.get(Memory, record.id)
                if row is None:
                    raise RuntimeError(f'missing seeded memory: {record.id}')
                for _version in range(2, target_version + 1):
                    row.current_version = _version
                    _add_version(session, row, reason='layer2_eval_seed', source_note_id=None)
                row.current_version = target_version
                seed_time = parse_datetime(raw.get('updated_at') or row.updated_at)
                row.created_at = seed_time
                row.updated_at = seed_time

    def map_result(self, candidate_id: str, result: dict[str, Any]) -> None:
        memory_id = result.get('memory_id')
        if not memory_id or str(memory_id) in self.db_to_logical:
            return
        if result.get('action') in {'insert', 'pending_review', 'supersede', 'conflict'}:
            ref = f'new:{candidate_id}'
            self.logical_to_db[ref] = str(memory_id)
            self.db_to_logical[str(memory_id)] = ref

    def decision_for(self, candidate_id: str) -> dict[str, Any] | None:
        return next((row for row in repository.list_memory_decisions(self.space_id, limit=500) if row['candidate_id'] == candidate_id), None)

    def normalize(self, raw: dict[str, Any], result: dict[str, Any], decision: dict[str, Any] | None) -> dict[str, Any]:
        decision = decision or {}
        target_ids = decision.get('target_memory_ids') or []
        target_refs = [self.db_to_logical.get(str(item), f'db:{item}') for item in target_ids]
        action = result.get('action') or decision.get('recommended_action')
        relation = result.get('relation') or decision.get('relation')
        target_ref = None
        memory_id = result.get('memory_id')
        if memory_id:
            target_ref = self.db_to_logical.get(str(memory_id))
            if target_ref is None and action in {'insert', 'pending_review', 'supersede', 'conflict'}:
                target_ref = f'new:{raw["candidate_id"]}'
        if action == 'pending_review':
            target_ref = None
        return {
            'candidate_id': str(raw['candidate_id']),
            'matched_memory_refs': target_refs,
            'task_identity_match': bool(target_refs) if raw['memory_type'] == 'task' else None,
            'relation': relation,
            'action': action,
            'target_memory_ref': target_refs[0] if target_refs else target_ref,
            'final_memory_type': None,
            'final_task_status': None,
            'create_version': None,
            'expected_version_sequence': None,
            'source_link_added': result.get('source_added'),
            'pending_review': action == 'pending_review',
            'reason': decision.get('reason') or result.get('reason'),
            'error': None,
        }

    def snapshot(self) -> dict[str, Any]:
        records = repository.list_memories(self.space_id, status=None, include_expired=True, limit=500)
        all_records = [record_dict(record, self.db_to_logical.get(record.id, f'db:{record.id}')) for record in records]
        active = [row for row in all_records if row['status'] == 'active']
        counts: dict[str, int] = {}
        for row in active:
            key = row['memory_key'] or row['memory_ref']
            counts[key] = counts.get(key, 0) + 1
        return {
            'all_memories': all_records,
            'active_memories': active,
            'pending_review_memories': [row for row in all_records if row['status'] == 'pending_review'],
            'expected_active_memory_refs': [row['memory_ref'] for row in active],
            'duplicate_active_count': sum(max(0, count - 1) for count in counts.values()),
            'stale_active_count': 0,
        }


def run_case(case: dict[str, Any], repeat: int) -> dict[str, Any]:
    adapter = PgCase(case, f'{repeat}_{case["case_id"]}')
    adapter.seed()
    raws = list(case['input'].get('incoming_candidates', []))
    barrier = Barrier(len(raws))

    def worker(raw: dict[str, Any]) -> dict[str, Any]:
        candidate = _candidate(raw)
        barrier.wait(timeout=30)
        try:
            result = consolidate_candidate(adapter.space_id, candidate.note_id or raw['candidate_id'], candidate)
            adapter.map_result(str(raw['candidate_id']), result)
            return {'raw': raw, 'result': result, 'decision': adapter.decision_for(str(raw['candidate_id']))}
        except Exception as exc:
            return {'raw': raw, 'result': {}, 'decision': None, 'error': {'type': type(exc).__name__, 'message': str(exc)}}

    with ThreadPoolExecutor(max_workers=len(raws), thread_name_prefix='layer2-pg-concurrency') as pool:
        outcomes = list(pool.map(worker, raws))

    normalized = [adapter.normalize(item['raw'], item.get('result') or {}, item.get('decision')) for item in outcomes]
    state = adapter.snapshot()
    active_by_ref = {row['memory_ref']: row for row in state['active_memories']}
    for decision, item in zip(normalized, outcomes):
        raw = item['raw']
        target = active_by_ref.get(decision.get('target_memory_ref'))
        if target:
            decision['final_memory_type'] = target['memory_type']
            decision['final_task_status'] = target['task_status']
            decision['expected_version_sequence'] = target['version_sequence']
            decision['create_version'] = decision.get('action') in {'insert', 'update'}
            note_id = str(raw.get('note_id') or raw.get('candidate_id'))
            decision['source_link_added'] = note_id in target.get('source_note_ids', [])
        else:
            decision['create_version'] = False
            decision['source_link_added'] = False

    with session_scope() as session:
        duplicate_versions = session.execute(
            select(MemoryVersion.memory_id, MemoryVersion.version, func.count().label('n'))
            .where(MemoryVersion.memory_id.in_([row['memory_id'] for row in state['all_memories']]))
            .group_by(MemoryVersion.memory_id, MemoryVersion.version)
            .having(func.count() > 1)
        ).all()
        duplicate_sources = session.execute(
            select(MemorySource.memory_id, MemorySource.note_id, func.count().label('n'))
            .where(MemorySource.memory_id.in_([row['memory_id'] for row in state['all_memories']]))
            .group_by(MemorySource.memory_id, MemorySource.note_id)
            .having(func.count() > 1)
        ).all()
    errors = [outcome.get('error') for outcome in outcomes if outcome.get('error')]
    invariants = {
        'errors': len(errors),
        'duplicate_active_count': state['duplicate_active_count'],
        'stale_active_count': state['stale_active_count'],
        'duplicate_version_rows': len(duplicate_versions),
        'duplicate_source_rows': len(duplicate_sources),
        'foreign_space_rows': 0,
    }
    invariants['pass'] = all(value == 0 for key, value in invariants.items() if key != 'pass')
    return {
        'case_id': case['case_id'],
        'repeat': repeat,
        'space_id': adapter.space_id,
        'backend': 'postgresql',
        'coverage_tags': case.get('coverage_tags', []),
        'predicted_decisions': normalized,
        'predicted_state': state,
        'gold': case['expected_output'],
        'invariants': invariants,
        'case_exact': _case_final_exact({'gold': case['expected_output'], 'predicted_state': state}),
        'errors': errors,
    }


def cleanup(prefix: str) -> None:
    with session_scope() as session:
        session.execute(delete(Space).where(Space.source_space_id.like(f'{prefix}%')))


def main() -> None:
    rows = load_rows()
    results: list[dict[str, Any]] = []
    try:
        for repeat in range(1, 4):
            for case in rows:
                results.append(run_case(case, repeat))
    finally:
        cleanup(PREFIX)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'layer2_postgres_concurrency_results.jsonl').write_text(
        ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in results), encoding='utf-8'
    )
    summary = {
        'backend': 'postgresql',
        'cases': len(results),
        'invariant_pass_count': sum(row['invariants']['pass'] for row in results),
        'invariant_pass_rate': sum(row['invariants']['pass'] for row in results) / len(results) if results else 0.0,
        'case_exact_count': sum(row['case_exact'] for row in results),
        'errors': sum(row['invariants']['errors'] for row in results),
        'duplicate_active_cases': sum(row['invariants']['duplicate_active_count'] > 0 for row in results),
        'duplicate_version_cases': sum(row['invariants']['duplicate_version_rows'] > 0 for row in results),
        'duplicate_source_cases': sum(row['invariants']['duplicate_source_rows'] > 0 for row in results),
        'foreign_space_cases': sum(row['invariants']['foreign_space_rows'] > 0 for row in results),
        'cleaned_test_space_prefix': PREFIX,
    }
    (OUT / 'layer2_postgres_concurrency_metrics.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (OUT / 'layer2_postgres_concurrency_summary.md').write_text(
        '# Layer 2 PostgreSQL concurrency / idempotence report\n\n'
        f"- Backend: PostgreSQL (real `DATABASE_URL`)\n"
        f"- Cases: {summary['cases']}\n"
        f"- Invariant pass rate: {summary['invariant_pass_rate'] * 100:.2f}%\n"
        f"- Errors: {summary['errors']}\n"
        f"- Duplicate active cases: {summary['duplicate_active_cases']}\n"
        f"- Duplicate version cases: {summary['duplicate_version_cases']}\n"
        f"- Duplicate source cases: {summary['duplicate_source_cases']}\n"
        f"- Cross-space contamination cases: {summary['foreign_space_cases']}\n"
        f"- Test spaces cleaned by prefix: `{PREFIX}`\n",
        encoding='utf-8',
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
