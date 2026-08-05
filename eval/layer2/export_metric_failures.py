from __future__ import annotations

import json
from pathlib import Path


ROOT = Path('/home/zcj/suixinji')
INPUT = ROOT / 'eval/results/layer2_final_repair/all/layer2_predictions.jsonl'
OUTPUT = ROOT / 'eval/results/layer2_final_repair/all'
DUPLICATE_TAGS = {
    'duplicate_delivery',
    'same_duplicate_source',
    'concurrent_same',
    'concurrent_conflict',
}


def load_cases() -> list[dict]:
    return [json.loads(line) for line in INPUT.read_text(encoding='utf-8').splitlines() if line.strip()]


def version_failures(cases: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for case in cases:
        for index, gold in enumerate(case.get('gold', {}).get('decisions', [])):
            pred = (case.get('predicted_decisions', [])[index]
                    if index < len(case.get('predicted_decisions', [])) else {})
            if gold.get('expected_version_sequence') == pred.get('expected_version_sequence'):
                continue
            rows.append({
                'case_id': case['case_id'],
                'dataset': case.get('dataset'),
                'candidate_id': gold.get('candidate_id'),
                'gold': gold,
                'pred': pred,
                'relation': {
                    'gold': gold.get('relation'),
                    'pred': pred.get('relation'),
                },
                'action': {
                    'gold': gold.get('action'),
                    'pred': pred.get('action'),
                },
                'create_version': {
                    'gold': gold.get('create_version'),
                    'pred': pred.get('create_version'),
                },
                'sequence': {
                    'gold': gold.get('expected_version_sequence'),
                    'pred': pred.get('expected_version_sequence'),
                },
                'coverage_tags': case.get('coverage_tags', []),
            })
    return rows


def source_failures(cases: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for case in cases:
        gold_final = case.get('gold', {}).get('final_memories', [])
        active = {row.get('memory_ref'): row for row in case.get('predicted_state', {}).get('active_memories', [])}
        all_rows = {row.get('memory_ref'): row for row in case.get('predicted_state', {}).get('all_memories', [])}
        mismatches: list[dict] = []
        for expected in gold_final:
            ref = expected.get('memory_ref')
            gold_sources = set(expected.get('source_note_ids', []))
            actual_active = set((active.get(ref) or {}).get('source_note_ids', []))
            actual_all = set((all_rows.get(ref) or {}).get('source_note_ids', []))
            if actual_active == gold_sources:
                continue
            decisions = [
                decision for decision in case.get('predicted_decisions', [])
                if ref in set(decision.get('matched_memory_refs') or [])
                or decision.get('target_memory_ref') == ref
            ]
            incoming_note_ids = {
                str(candidate.get('note_id') or candidate.get('candidate_id'))
                for candidate in case.get('input', {}).get('incoming_candidates', [])
            }
            observed_added = sorted((actual_all - gold_sources) & incoming_note_ids)
            mismatches.append({
                'memory_ref': ref,
                'gold_source_set': sorted(gold_sources),
                # This is the set used by the current source_exact_set metric:
                # it indexes active_memories only.
                'actual_source_set': sorted(actual_active),
                # Included to distinguish a metric coverage issue from a write issue.
                'actual_all_memory_source_set': sorted(actual_all),
                'source_added': {
                    'decision_field_values': [decision.get('source_link_added') for decision in decisions],
                    'observed_in_all_memory': bool(observed_added),
                    'observed_note_ids': observed_added,
                },
                'decisions': [
                    {
                        'candidate_id': decision.get('candidate_id'),
                        'relation': decision.get('relation'),
                        'action': decision.get('action'),
                        'target_memory_ref': decision.get('target_memory_ref'),
                        'source_link_added': decision.get('source_link_added'),
                    }
                    for decision in decisions
                ],
            })
        if mismatches:
            rows.append({
                'case_id': case['case_id'],
                'dataset': case.get('dataset'),
                'coverage_tags': case.get('coverage_tags', []),
                'is_duplicate_delivery': bool(set(case.get('coverage_tags', [])) & DUPLICATE_TAGS),
                'memory_mismatches': mismatches,
            })
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')


def md_escape(value: object) -> str:
    return str(value).replace('|', '\\|').replace('\n', ' ')


def write_markdown(version_rows: list[dict], source_rows: list[dict]) -> None:
    lines = [
        '# Layer 2 Version / Source Exact 失败样本',
        '',
        '输入：`layer2_predictions.jsonl`；生成时间由文件系统记录。',
        '',
        '## Version Sequence 失败',
        '',
        f'- 失败 decision：{len(version_rows)}；失败 case：{len({row["case_id"] for row in version_rows})}',
        '- `Case Exact` 使用最终状态，并对 terminal memory 忽略 version；本表按 decision 的 sequence 严格比较。',
        '',
        '| case_id | Gold sequence | Pred sequence | Relation | Action | Gold create_version | Pred create_version |',
        '|---|---:|---:|---|---|---:|---:|',
    ]
    for row in version_rows:
        lines.append(
            f"| {row['case_id']} | {md_escape(row['sequence']['gold'])} | {md_escape(row['sequence']['pred'])} | "
            f"{row['relation']['gold']} → {row['relation']['pred']} | {row['action']['gold']} → {row['action']['pred']} | "
            f"{row['create_version']['gold']} | {row['create_version']['pred']} |"
        )
    lines.extend([
        '',
        '## Source Exact 失败',
        '',
        f'- 失败 case：{len(source_rows)}；是否重复投递按 coverage tag 判定。',
        '- `actual_source_set` 是当前指标实际比较的 active memory source 集合；`actual_all_memory_source_set` 用于显示 terminal/superseded memory 中是否仍保留证据。',
        '',
        '| case_id | memory_ref | Gold Source | Actual active Source | Actual all-memory Source | source_added | 重复投递 |',
        '|---|---|---|---|---|---|---|',
    ])
    for row in source_rows:
        for mismatch in row['memory_mismatches']:
            added = mismatch['source_added']
            lines.append(
                f"| {row['case_id']} | {mismatch['memory_ref']} | {md_escape(mismatch['gold_source_set'])} | "
                f"{md_escape(mismatch['actual_source_set'])} | {md_escape(mismatch['actual_all_memory_source_set'])} | "
                f"{md_escape(added['observed_note_ids'])} | {row['is_duplicate_delivery']} |"
            )
    (OUTPUT / 'layer2_metric_failures.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> None:
    cases = load_cases()
    version_rows = version_failures(cases)
    source_rows = source_failures(cases)
    write_jsonl(OUTPUT / 'layer2_version_sequence_failures.jsonl', version_rows)
    write_jsonl(OUTPUT / 'layer2_source_exact_failures.jsonl', source_rows)
    write_markdown(version_rows, source_rows)
    print(json.dumps({
        'version_failure_decisions': len(version_rows),
        'version_failure_cases': len({row['case_id'] for row in version_rows}),
        'source_failure_cases': len(source_rows),
        'output': str(OUTPUT),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
