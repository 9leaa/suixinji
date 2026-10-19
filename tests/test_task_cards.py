"""Task extraction -> versioned storage -> retrieval evidence, with deterministic model responses."""
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from core import settings
from memory import extractor, service, repository as repo, task_cards as cards, workflow_state
from memory.models import MemoryCandidate, MemoryDecision
from memory.service import process_note_memory, memory_search
from memory.task_schema import TaskInput


@pytest.fixture(autouse=True)
def task_mode(monkeypatch):
    monkeypatch.setattr(settings, 'TASK_CARDS_ENABLED', True)
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'llm')
    monkeypatch.setattr(extractor, 'MEMORY_EXTRACTOR_MODE', 'llm')
    monkeypatch.setattr(service, 'MEMORY_EXTRACTOR_MODE', 'llm')
    monkeypatch.setattr(extractor, '_LLM_CONNECTION_CIRCUIT_OPEN_UNTIL', 0)
    monkeypatch.setattr(cards, 'complete_json', Mock(side_effect=AssertionError('unexpected association call')))


def detail(text, kind='requirement', **kw):
    return {'kind': kind, 'condition': '', 'description': text, 'evidence_span': text, **kw}


def row(text='记得提交项目周报', **kw):
    return {'memory_type': 'task', 'subject': '用户', 'task': '提交项目周报', 'instance': None,
            'task_status': 'todo', 'closure_reason': None, 'details': [], 'evidence_span': text, **kw}


def write(monkeypatch, text='记得提交项目周报', *, key='n1', data=None, previous=None, owner=None, ts='2026-10-19T12:00:00+08:00'):
    mock = Mock(return_value={'candidates': [data or row(text)]})
    monkeypatch.setattr(extractor, 'complete_json', mock)
    report = process_note_memory({'id': key, 'space_id': 's', 'text': text, 'ts': ts,
        'user_id': owner, 'previous_messages': previous or []})
    assert mock.call_count == 1
    return report


def result(report):
    assert report['extraction_status'] == 'completed', report
    assert len(report['results']) == 1, report
    return report['results'][0]


def test_new_card_and_source_time_flow_into_query_and_ask(monkeypatch):
    text = '记得提交项目周报，周五前交，还等销售数据'
    data = row(text, instance='2026年第43周', details=[detail('周五前交', 'time'), detail('还等销售数据', 'blocker')])
    saved = result(write(monkeypatch, text, data=data))
    card = saved['task_card']
    assert (card['subject'], card['task'], card['instance'], card['task_status']) == ('用户', '提交项目周报', '2026年第43周', 'todo')
    assert card['details'][0]['sources'][0]['message_time'] == '2026-10-19T12:00:00+08:00'
    assert card['details'][0]['source_ids'] == [card['sources'][0]['id']]
    assert card['sources'][0]['original_text'] == text
    items = memory_search('s', '项目周报', memory_type='task', min_score=0)
    assert items[0]['task'] == '提交项目周报'
    from agent.ask_executor import _as_evidence
    # Evidence adapter retains the structured task rather than only its summary.
    item = _as_evidence(items[0], tool='memory_search', role='supporting')
    assert item.task_card['instance'] == '2026年第43周'
    from agent.ask_models import UnitEvidenceBundle
    from agent.ask_workflow import _bundle_payload
    bundle = UnitEvidenceBundle(unit_id='u1', evidence=[item], resolution={'status': 'resolved', 'reason_code': 'task_found'})
    assert _bundle_payload(bundle)['evidence'][0]['task_card']['details'][0]['sources'][0]['message_time'] == '2026-10-19T12:00:00+08:00'


def test_no_legacy_fields_or_second_extraction(monkeypatch):
    saved = result(write(monkeypatch))
    record = repo.get_memory(saved['memory_id'])
    assert record.memory_key_version == cards.VERSION
    assert 'operation' not in record.scope
    payload = json.loads(extractor.complete_json.call_args.kwargs['user_prompt'])
    assert payload['message_time'] == '2026-10-19T12:00:00+08:00'
    cards.complete_json.assert_not_called()


def test_instance_separation_and_ambiguous_completion(monkeypatch):
    first = result(write(monkeypatch, data=row(instance='第43周')))
    second = result(write(monkeypatch, key='n2', data=row(instance='第44周')))
    assert first['memory_id'] != second['memory_id']
    pending = result(write(monkeypatch, '周报交了', key='n3', data=row('周报交了', task_status='done', closure_reason='completed')))
    assert pending['action'] == 'pending_review'
    assert all(r.task_status == 'todo' for r in repo.list_memories('s'))
    done = result(write(monkeypatch, '第43周的周报交了', key='n4', data=row('第43周的周报交了', instance='第43周', task_status='done', closure_reason='completed')))
    assert done['memory_id'] == first['memory_id']
    assert repo.get_memory(second['memory_id']).status == 'active'
    assert repo.get_memory(second['memory_id']).task_status == 'todo'


def test_reschedule_and_clear_blocker_keep_other_requirements_and_history(monkeypatch):
    text = '提交周报，包含收入，周五前交，等待销售数据'
    saved = result(write(monkeypatch, text, data=row(text, details=[detail('包含收入'), detail('周五前交', 'time'), detail('等待销售数据', 'blocker')])))
    record = repo.get_memory(saved['memory_id'])
    old = record.scope['task_card']['details']
    plan = {'action': 'update', 'target_id': record.id, 'replacements': [{'index': 0, 'detail_id': old[1]['id']}], 'retire_ids': [old[2]['id']]}
    monkeypatch.setattr(cards, 'complete_json', Mock(return_value=plan))
    text = '周报改成周六交，销售数据已经拿到了'
    updated = result(write(monkeypatch, text, key='n2', data=row(text, intent='update', details=[detail('周六交', 'time'), detail('销售数据已经拿到了', 'progress')])))
    assert updated['memory_id'] == record.id
    assert {d['description'] for d in updated['task_card']['details']} == {'包含收入', '周六交', '销售数据已经拿到了'}
    latest = repo.get_memory(record.id)
    assert latest.current_version == 2 and latest.task_status == 'todo'
    assert len([d for d in latest.scope['task_card']['details'] if d['status'] == 'history']) == 2
    timeline = repo.get_memory_timeline('s', memory_id=record.id)
    assert len(timeline[0]['details']) == 5


@pytest.mark.parametrize('reason,label', [('completed', '已完成'), ('cancelled', '已取消'), ('abandoned', '已放弃'), (None, '原因未说明')])
def test_two_states_preserve_closure_and_do_not_reopen_implicitly(monkeypatch, reason, label):
    saved = result(write(monkeypatch))
    ended = result(write(monkeypatch, '这件事结束了', key='n2', data=row('这件事结束了', task_status='done', closure_reason=reason)))
    assert ended['memory_id'] == saved['memory_id']
    assert ended['task_card']['task_status'] == 'done'
    assert label in ended['task_card']['content']
    pending = result(write(monkeypatch, '周报继续处理', key='n3', data=row('周报继续处理')))
    assert pending['action'] == 'pending_review'
    reopened = result(write(monkeypatch, '重开这次周报任务', key='n4', data=row('重开这次周报任务', intent='reopen')))
    assert reopened['memory_id'] == saved['memory_id']
    assert reopened['task_card']['closure_reason'] is None


def test_done_task_can_receive_new_result(monkeypatch):
    saved = result(write(monkeypatch, '周报已完成', data=row('周报已完成', task_status='done', closure_reason='completed')))
    monkeypatch.setattr(cards, 'complete_json', Mock(return_value={'action': 'update', 'target_id': saved['memory_id']}))
    updated = result(write(monkeypatch, '周报已归档到共享盘', key='n2', data=row('周报已归档到共享盘', task_status='done', details=[detail('周报已归档到共享盘', 'outcome')])))
    assert updated['task_card']['closure_reason'] == 'completed'
    assert updated['task_card']['details'][0]['description'] == '周报已归档到共享盘'
    assert repo.get_memory(saved['memory_id']).current_version == 2


@pytest.mark.parametrize('changes', [{'task': ''}, {'task_status': 'blocked'}, {'task_status': 'todo', 'closure_reason': 'completed'}, {'entity': '旧字段'}, {'unresolved_reason': '无法确定他指谁'}, {'details': [detail('原文没有这句话')]}])
def test_bad_rows_become_pending_instead_of_silent_loss(monkeypatch, changes):
    saved = result(write(monkeypatch, data=row(**changes)))
    assert saved['action'] == 'pending_review'
    assert not repo.list_memories('s')
    assert workflow_state.list_items('s', 'notification')


def test_subject_owner_and_message_reference_boundaries(monkeypatch):
    a = result(write(monkeypatch, owner='alice'))
    b = result(write(monkeypatch, key='n2', owner='bob'))
    c = result(write(monkeypatch, '小明要提交项目周报', key='n3', data=row('小明要提交项目周报', subject='同事小明')))
    assert len({a['memory_id'], b['memory_id'], c['memory_id']}) == 3
    pending = result(write(monkeypatch, '他做完了', key='n4', data=row('他做完了', subject='同事小明', task_status='done', antecedent_note_id='forged', antecedent_evidence_span='小明')))
    assert pending['action'] == 'pending_review'
    assert repo.get_memory(c['memory_id']).task_status == 'todo'


def test_older_message_and_invalid_detail_target_cannot_overwrite(monkeypatch):
    saved = result(write(monkeypatch))
    old = result(write(monkeypatch, '周报做完了', key='old', ts='2026-10-18T12:00:00+08:00', data=row('周报做完了', task_status='done')))
    assert old['action'] == 'pending_review'
    monkeypatch.setattr(cards, 'complete_json', Mock(return_value={'action': 'update', 'target_id': saved['memory_id'], 'retire_ids': ['foreign-detail']}))
    bad = result(write(monkeypatch, '补充周报要求', key='bad', data=row('补充周报要求', details=[detail('补充周报要求')])) )
    assert bad['action'] == 'pending_review'
    assert repo.get_memory(saved['memory_id']).current_version == 1


def test_repository_rejects_stale_same_status_card_update(monkeypatch):
    saved = result(write(monkeypatch))
    record = repo.get_memory(saved['memory_id'])
    candidate = MemoryCandidate('task', record.content, .6, .9, task_status='todo', memory_key=record.memory_key,
        memory_key_version=cards.VERSION, scope=record.scope, candidate_id='stale')
    decision = MemoryDecision(candidate_id='stale', relation='update_task', recommended_action='update_task',
        target_memory_ids=[record.id], target_snapshot_version=0, confidence=.9, reason='test', evidence=[])
    with pytest.raises(cards.TaskVersionConflict):
        repo.apply_memory_decision('s', 'n2', candidate, decision)
    assert repo.get_memory(record.id).current_version == 1


def test_legacy_record_is_readable_and_explicitly_upgraded_in_place(monkeypatch):
    legacy = repo.insert_memory('s', MemoryCandidate('task', '记得提交项目周报', .6, .9, subject='用户',
        predicate='周报', object_value='提交项目周报', task_status='todo', scope={'canonical_topic': '提交项目周报'}), source_note_id='legacy')
    saved = result(write(monkeypatch, '周报提交了', data=row('周报提交了', task_status='done', closure_reason='completed')))
    assert saved['memory_id'] == legacy.id
    assert repo.get_memory(legacy.id).memory_key_version == cards.VERSION
    assert len(repo.get_memory(legacy.id).versions) >= 1


def test_same_source_replay_does_not_duplicate_details(monkeypatch):
    text = '周报要包含收入'
    saved = result(write(monkeypatch, text, data=row(text, details=[detail('包含收入')])))
    candidate = cards.candidate_from_row('n1', text, row(text, details=[detail('包含收入')]))
    candidate = replace(candidate, candidate_id='replay')
    note = {'id': 'n1', 'space_id': 's', 'text': text, 'ts': '2026-10-19T12:00:00+08:00'}
    again = cards.apply_task(candidate, note)
    assert len(again['task_card']['details']) == 1
    assert len(again['task_card']['sources']) == 1
    assert again['memory_id'] == saved['memory_id']


def test_schema_does_not_allow_model_source_ids():
    with pytest.raises(ValidationError):
        TaskInput.model_validate(row(details=[{**detail('原文'), 'source_ids': ['fake']}]))


def test_concurrent_progress_does_not_lose_either_detail(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    saved = result(write(monkeypatch))
    gate = threading.Barrier(2)
    calls = {}
    lock = threading.Lock()

    def plan(**kwargs):
        payload = json.loads(kwargs['user_prompt'])
        thread = threading.get_ident()
        with lock:
            count = calls.get(thread, 0)
            calls[thread] = count + 1
        if count == 0:
            gate.wait(timeout=5)
        return {'action': 'update', 'target_id': payload['cards'][0]['id']}
    monkeypatch.setattr(cards, 'complete_json', plan)

    def update(index):
        text = f'进展{index}已确认'
        data = row(text, details=[detail(text, 'progress')])
        note = {'id': f'parallel-{index}', 'space_id': 's', 'text': text, 'ts': '2026-10-19T12:00:00+08:00'}
        return cards.apply_task(cards.candidate_from_row(note['id'], text, data), note)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [1, 2]))
    assert all(r['action'] == 'update_task' for r in results)
    final = repo.get_memory(saved['memory_id'])
    assert final.current_version == 3
    assert {d['description'] for d in cards.active_details(final.scope['task_card'])} == {'进展1已确认', '进展2已确认'}


def test_hybrid_keeps_direct_schema_and_mixed_memory_types(monkeypatch):
    monkeypatch.setattr(extractor, 'MEMORY_EXTRACTOR_MODE', 'hybrid')
    text = '记得提交项目周报；我住在北京'
    rows = [row('记得提交项目周报'), {'memory_type': 'semantic', 'entity': '用户', 'attribute': 'location',
        'new_value': '北京', 'canonical_topic': '居住地', 'content': '用户住在北京', 'evidence_span': '我住在北京',
        'confidence': .9, 'importance': .6}]
    monkeypatch.setattr(extractor, 'complete_json', Mock(return_value={'candidates': rows}))
    candidates = extractor.extract_candidates('mixed', text)
    assert [c.memory_type for c in candidates].count('task') == 1
    assert {c.memory_type for c in candidates} == {'task', 'semantic'}
    assert 'task_input' in next(c for c in candidates if c.memory_type == 'task').scope


def test_reference_keeps_first_stage_identity_and_clarification_context(monkeypatch):
    saved = result(write(monkeypatch, data=row(instance='第43周')))
    prior = [{'note_id': 'n1', 'text': '第43周要提交项目周报', 'role': 'user'}]
    text = '这个交好了'
    done = result(write(monkeypatch, text, key='done', data=row(text, instance='第43周', task_status='done',
        closure_reason='completed', antecedent_note_id='n1', antecedent_evidence_span='第43周要提交项目周报'), previous=prior))
    assert done['memory_id'] == saved['memory_id']
    assert done['task_card']['sources'][-1]['antecedent_note_id'] == 'n1'


def test_explicit_new_occurrence_does_not_reopen_completed_task(monkeypatch):
    ended = result(write(monkeypatch, '周报已完成', data=row('周报已完成', task_status='done', closure_reason='completed')))
    new = result(write(monkeypatch, '再写一份周报', key='n2', data=row('再写一份周报', intent='new')))
    assert new['memory_id'] != ended['memory_id']
    assert repo.get_memory(ended['memory_id']).task_status == 'done'


def test_model_failure_keeps_original_in_retry_pending(monkeypatch):
    monkeypatch.setattr(extractor, 'complete_json', Mock(side_effect=RuntimeError('provider unavailable')))
    with pytest.raises(RuntimeError):
        process_note_memory({'id': 'failed', 'space_id': 's', 'text': '记得提交周报'})
    pending = workflow_state.list_items('s', 'pending')
    assert pending[0]['note']['text'] == '记得提交周报'
    assert pending[0]['code'] == 'processing_failed'
    assert not repo.list_memories('s')


def test_cancelled_task_stays_available_to_inventory(monkeypatch):
    from agent.ask_executor import _task_source_supports_inventory
    item = {'memory_key_version': cards.VERSION, 'closure_reason': 'cancelled', 'sources': [{'note_id': 'n1'}]}
    monkeypatch.setattr('agent.query_agent.get_note_for_evidence', lambda *args: {'text': '这份周报不需要处理'})
    assert _task_source_supports_inventory('s', item)


def test_two_instances_in_one_message_are_not_deduplicated(monkeypatch):
    text = '这两周都要提交项目周报'
    monkeypatch.setattr(extractor, 'complete_json', Mock(return_value={'candidates': [
        row(text, instance='第43周'), row(text, instance='第44周')]}))
    report = process_note_memory({'id': 'two', 'space_id': 's', 'text': text, 'ts': '2026-10-19T12:00:00+08:00'})
    assert [r['action'] for r in report['results']] == ['insert', 'insert']
    assert {r.scope['task_card']['instance'] for r in repo.list_memories('s')} == {'第43周', '第44周'}


def test_task_and_preference_cards_can_be_enabled_together(monkeypatch):
    monkeypatch.setattr(settings, 'PREFERENCE_CARDS_ENABLED', True)
    text = '我喜欢咖啡；记得提交项目周报'
    rows = [row('记得提交项目周报'), {'memory_type': 'preference', 'subject': '用户', 'preference_object': '咖啡',
        'attitude': '喜欢', 'condition': '', 'description': '喜欢咖啡', 'is_general': True, 'evidence_span': '我喜欢咖啡'}]
    monkeypatch.setattr(extractor, 'complete_json', Mock(return_value={'candidates': rows}))
    report = process_note_memory({'id': 'both', 'space_id': 's', 'text': text})
    assert len(report['results']) == 2 and all(r['action'] == 'insert' for r in report['results'])
    assert {r.memory_key_version for r in repo.list_memories('s')} == {cards.VERSION, 'preference-card-v1'}
