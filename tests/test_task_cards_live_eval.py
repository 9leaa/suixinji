"""Scoring must tolerate equivalent names without hiding identity or date errors."""
from scripts.eval_task_cards_live import check


def card(subject='用户', status='todo', instance=None):
    return {'id': subject, 'subject': subject, 'task_status': status,
            'closure_reason': None, 'instance': instance, 'details': []}


def test_subject_alias_keeps_identity_and_state_checks():
    expected = {'cards': 2, 'subjects': ['用户', '同事小明'],
                'subject_aliases': {'小明': '同事小明'},
                'subject_status': {'用户': 'todo', '同事小明': 'done'}}
    assert check(expected, [card(), card('小明', 'done')], [], []) == []
    assert check(expected, [card(), card('小明')], [], [])
    assert check(expected, [card(), card('小明', 'done'), card('同事小明')], [], [])


def test_calendar_ranges_ignore_wording_but_reject_rolling_seven_days():
    expected = {'cards': 1, 'instance_date_ranges': [['2026-10-19', '2026-10-25']]}
    assert check(expected, [card(instance='2026-10-19至2026-10-25这一周的周报')], [], []) == []
    assert check(expected, [card(instance='2026-10-20至2026-10-26当周的周报')], [], [])


def test_pending_and_resolved_blocker_are_not_counted_as_success():
    expected = {'cards': 1, 'no_active_kinds': ['blocker']}
    saved = card()
    saved['details'] = [{'kind': 'blocker', 'condition': '', 'description': '阻碍已解除'}]
    errors = check(expected, [saved], [], [{'action': 'pending_review'}])
    assert len(errors) == 2


def test_persisted_pending_counts_even_when_service_results_are_empty():
    expected = {'cards': 0, 'pending': True}
    assert check(expected, [], [], [], pending_for_note=True) == []
    assert check(expected, [], [], []) == ['预期待确认']
    assert check({'cards': 0}, [], [], [], pending_for_note=True) == ['不应进入待确认']


def test_saved_pending_is_grouped_by_originating_note(tmp_path):
    import json
    import sqlite3
    from scripts.eval_task_cards_live import saved_pending_by_note

    with sqlite3.connect(tmp_path / 'memory.db') as conn:
        conn.execute('CREATE TABLE memory_workflow_state (kind TEXT, body TEXT)')
        for note_id in ['earlier-note', 'current-note']:
            conn.execute('INSERT INTO memory_workflow_state VALUES (?, ?)',
                         ('pending', json.dumps({'note': {'id': note_id}, 'status': 'pending'})))
    grouped = saved_pending_by_note(tmp_path)
    assert len(grouped['current-note']) == 1
    assert grouped['current-note'][0]['note']['id'] == 'current-note'
    assert grouped.get('later-note', []) == []
