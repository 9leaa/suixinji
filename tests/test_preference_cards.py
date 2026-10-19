from datetime import datetime, timedelta
import json

import pytest

from core import settings
from memory import preference_cards as cards, repository as repo, workflow_state as state
from memory.models import MemoryCandidate
from memory.service import process_note_memory, memory_search
from memory.workflow import create_pending, deliver_notifications, review_issue, export_cases, capture_issue


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, 'PREFERENCE_CARDS_ENABLED', True)
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'rules')


def note(text, key='n1', **kwargs):
    return {'id': key, 'space_id': 's', 'text': text, 'ts': '2026-10-18T10:00:00+08:00', **kwargs}


def write(text, key='n1', **kwargs):
    return process_note_memory(note(text, key, **kwargs))


def card(target='咖啡', subject='用户'):
    record = cards.find_card('s', subject, target)
    assert record is not None
    return record.scope['preference_card']


def test_general_and_exceptions_are_one_card():
    for i, text in enumerate(['我喜欢喝咖啡', '我不喜欢在早上喝咖啡', '我喜欢在晚上喝咖啡']):
        write(text, str(i))
    value = card()
    assert value['attitude'] == '喜欢'
    assert [(d['attitude'], d['condition']) for d in cards.active_details(value)] == [('喜欢', ''), ('不喜欢', '早上'), ('喜欢', '晚上')]
    hits = memory_search('s', '我喜欢咖啡吗', memory_type='preference')
    assert len(hits) == 1
    assert len(hits[0]['details']) == 3
    assert all(s['message_time'] == '2026-10-18T10:00:00+08:00' for s in hits[0]['sources'])


def test_subjects_and_pronoun_resolution():
    write('我的同事小明喜欢咖啡')
    write('他不喜欢低糖咖啡', 'n2', previous_messages=[{'note_id': 'n1', 'text': '我的同事小明喜欢咖啡', 'role': 'user'}])
    assert len(cards.active_details(card(subject='同事小明'))) == 2
    assert cards.find_card('s', '用户', '咖啡') is None
    result = write('他喜欢咖啡', 'n3', previous_messages=[{'note_id': 'p', 'text': '同事小明，领导老王', 'role': 'user'}])
    assert result['results'][0]['action'] == 'pending_review'
    pending = state.list_items('s', 'pending')[0]
    assert '他喜欢咖啡' in state.list_items('s', 'notification')[0]['text']
    write(f"/pending resolve {pending['id']} 同事小明", 'resolve')
    assert state.get('s', 'pending', pending['id'])['status'] == 'resolved'


def test_object_alias_and_subtype_are_distinct_but_related():
    write('我喜欢coffee')
    write('我喜欢拿铁咖啡', 'n2')
    write('我喜欢拿铁', 'n3')
    assert len(cards.active_details(card())) == 2
    assert card('拿铁')['preference_object'] == '拿铁'
    hits = memory_search('s', '我喜欢咖啡吗', memory_type='preference')
    assert {h['preference_object'] for h in hits} == {'咖啡', '拿铁'}


def test_negative_without_prior_positive_and_duplicates():
    write('我不喜欢咖啡')
    write('我不喜欢咖啡', 'n2')
    write('我不喜欢咖啡', 'n2')
    assert card()['attitude'] == '不喜欢'
    assert len(card()['details']) == 1
    assert len(card()['details'][0]['sources']) == 2


def test_change_keeps_history_and_conditions():
    write('我不喜欢早上喝咖啡')
    write('我现在喜欢早上喝咖啡了', 'n2', ts='2026-10-19T10:00:00+08:00')
    assert [d['status'] for d in card()['details']] == ['history', 'active']
    assert card()['attitude'] == '未说明'


def test_hard_limit_pending_never_deletes_facts(monkeypatch):
    monkeypatch.setattr(settings, 'PREFERENCE_CARD_HARD_LIMIT', 2)
    write('我喜欢咖啡')
    write('我喜欢低糖咖啡', 'n2')
    report = write('我喜欢晚上喝咖啡', 'n3')
    assert report['results'][0]['action'] == 'pending_review'
    cards.run_consolidation('s', force=True)
    assert len(cards.active_details(card())) == 2
    assert state.list_items('s', 'pending')[0]['status'] == 'pending'


def test_forget_masks_original_and_prevents_replay():
    write('我喜欢喝咖啡')
    write('请忘记我喜欢喝咖啡', 'forget')
    assert not cards.active_details(card())
    assert memory_search('s', '咖啡', memory_type='preference') == []
    assert '喜欢喝咖啡' not in cards.mask_note('s', note('我喜欢喝咖啡'))['text']
    candidate = MemoryCandidate('preference', '我喜欢喝咖啡', .8, .9, evidence_span='我喜欢喝咖啡')
    assert cards.apply_preference(candidate, note('我喜欢喝咖啡'))['action'] == 'discard'
    write('我喜欢喝咖啡', 'fresh')
    assert len(cards.active_details(card())) == 1


def test_ambiguous_forget_is_pending():
    write('我喜欢咖啡')
    write('我喜欢晚上喝咖啡', 'n2')
    result = write('请忘记我喜欢咖啡', 'forget')
    assert result['results'][0]['action'] == 'pending_review'
    assert len(cards.active_details(card())) == 2


def test_notifications_retry_without_duplication():
    pending = create_pending('s', note('他喜欢咖啡'), '主体不清楚')
    create_pending('s', note('他喜欢咖啡'), '主体不清楚')
    def failed(*_):
        raise OSError('offline')
    with pytest.raises(OSError):
        deliver_notifications('s', failed)
    deliveries = []
    deliver_notifications('s', lambda *args: deliveries.append(args))
    deliver_notifications('s', lambda *args: deliveries.append(args))
    assert len(deliveries) == 1
    assert deliveries[0][0] == pending['id']


def test_only_reviewed_cases_are_exported(tmp_path):
    issue = capture_issue('s', stage='write', reason='wrong target', snapshot={'original': '我喜欢咖啡'})
    path = tmp_path / 'cases.jsonl'
    assert export_cases('s', path) == 0
    with pytest.raises(ValueError):
        review_issue('s', issue['id'], status='confirmed', reviewer='owner')
    review_issue('s', issue['id'], status='confirmed', expected={'subject': '用户'}, reviewer='owner')
    assert export_cases('s', path) == 1
    assert json.loads(path.read_text())['expected'] == {'subject': '用户'}


def test_daily_dirty_consolidation_does_not_loop():
    write('我喜欢咖啡')
    now = datetime.now().astimezone() + timedelta(days=2)
    assert len(cards.run_consolidation('s', now=now)) == 1
    assert cards.run_consolidation('s', now=now + timedelta(days=2)) == []


def test_owner_and_space_isolation():
    write('我喜欢咖啡', user_id='alice')
    assert memory_search('s', '咖啡', memory_type='preference', access_context={'requester': 'bob'}) == []
    assert len(memory_search('s', '咖啡', memory_type='preference', access_context={'requester': 'alice'})) == 1
    assert memory_search('other', '咖啡', memory_type='preference', access_context={'requester': 'alice'}) == []


def test_neutral_and_model_schema_failure(monkeypatch):
    write('我对咖啡无所谓')
    assert card()['attitude'] == '无所谓'
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'llm')
    monkeypatch.setattr(cards, 'complete_json', lambda **_: {'subject': '用户', 'preference_object': '茶'})
    candidate = MemoryCandidate('preference', '我喜欢茶', .8, .9, evidence_span='我喜欢茶')
    assert cards.apply_preference(candidate, note('我喜欢茶', 'n2'))['action'] == 'pending_review'


def test_stale_snapshot_cannot_drop_new_details():
    write('我喜欢咖啡')
    old = cards.find_card('s', '用户', '咖啡')
    write('我喜欢低糖咖啡', 'n2')
    with pytest.raises(cards.CardVersionConflict):
        cards._persist('s', old.scope['preference_card'], old, {'note_id': 'n1'}, 'stale-operation', reason='test')
    assert len(card()['details']) == 2


def test_history_and_ask_evidence_do_not_leak_forgotten_details():
    from agent.ask_executor import _as_evidence
    write('我喜欢咖啡')
    hit = memory_search('s', '咖啡', memory_type='preference')[0]
    item = _as_evidence(hit, tool='memory_search', role='supporting')
    assert item.preference_details[0]['sources'][0]['original_text'] == '我喜欢咖啡'
    write('请忘记我喜欢咖啡', 'forget')
    assert repo.get_memory_timeline('s') == []
    serialized = cards.find_card('s', '用户', '咖啡').to_dict()
    assert serialized['details'] == [] and serialized['sources'] == [] and serialized['versions'] == []


def test_consolidation_rejects_cross_condition_model_merge(monkeypatch):
    write('我喜欢早上喝咖啡')
    write('我喜欢晚上喝咖啡', 'n2')
    before = card()
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'llm')
    from memory import preference_consolidation as review
    sources = {s['note_id']: {'id': s['note_id'], 'text': s['original_text']} for d in before['details'] for s in d['sources']}
    monkeypatch.setattr('storage.note_storage.find_note', lambda space, key: sources.get(key))
    monkeypatch.setattr(review, 'complete_json', lambda **_: {'assessments': [{'detail_id': d['id'], 'verdict': 'supported', 'quote': d['sources'][0]['original_text'], 'source_ids': [d['sources'][0]['id']], 'reason': '原文明确'} for d in before['details']], 'relations': [], 'actions': [{
        'action': 'deduplicate', 'card_id': cards.find_card('s', '用户', '咖啡').id,
        'detail_ids': [d['id'] for d in before['details']],
        'source_ids': [s['id'] for d in before['details'] for s in d['sources']], 'reason': '错误合并'}]})
    result = cards.run_consolidation('s', force=True)
    assert result[0]['action'] == 'failed'
    assert card() == before
    assert state.list_items('s', 'issue')


def test_recovery_after_write_before_dirty_marker(monkeypatch):
    write('我喜欢咖啡')
    record = cards.find_card('s', '用户', '咖啡')
    job = state.get('s', 'card_job', record.id)
    state.put('s', 'card_job', record.id, {**job, 'dirty': False})
    assert len(cards.run_consolidation('s', force=True)) == 1


def test_local_notification_adapter(monkeypatch):
    from memory.runtime_workflow import register_route, flush_notifications
    from bot import feishu_bot
    sent = []
    monkeypatch.setattr(feishu_bot, 'send_text', lambda *args: sent.append(args))
    register_route('s', 'n1', chat_id='test-chat')
    create_pending('s', note('他喜欢咖啡'), '主体不明确')
    assert flush_notifications('s') == 1
    assert flush_notifications('s') == 0
    assert len(sent) == 1 and sent[0][0] == 'test-chat'


def test_any_message_task_failure_is_pending_and_recovery_notifies(monkeypatch):
    from memory.runtime_workflow import guarded_handler
    monkeypatch.setattr('repositories.postgres.dispatch.load_inbox_record', lambda _: {
        'id': 'n1', 'text': '查询我的任务', 'chat_id': 'test-chat'})
    monkeypatch.setattr('memory.runtime_workflow.flush_notifications', lambda _: None)
    task = {'id': 'task1', 'space_id': 's', 'task_type': 'query', 'payload_json': {'inbox_id': 'n1'}}
    def fail(_):
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        guarded_handler(fail)(task)
    pending = state.list_items('s', 'pending')[0]
    assert pending['stage'] == 'query' and pending['note']['text'] == '查询我的任务'
    guarded_handler(lambda _: 'answer')(task)
    assert state.get('s', 'pending', pending['id'])['status'] == 'resolved'


def test_legacy_migration_preserves_original_and_time(monkeypatch):
    from memory.preference_migration import migrate_legacy
    monkeypatch.setattr(settings, 'PREFERENCE_CARDS_ENABLED', False)
    write('我喜欢咖啡')
    legacy = repo.list_memories('s')[0]
    monkeypatch.setattr(settings, 'PREFERENCE_CARDS_ENABLED', True)
    monkeypatch.setattr('storage.note_storage.find_note', lambda *_: note('我喜欢咖啡'))
    assert migrate_legacy('s')[0]['status'] == 'planned'
    assert cards.find_card('s', '用户', '咖啡') is None
    assert migrate_legacy('s', apply=True)[0]['status'] == 'migrated'
    assert repo.get_memory(legacy.id).status == 'archived'
    assert card()['details'][0]['sources'][0]['message_time'] == '2026-10-18T10:00:00+08:00'
    assert len(repo.get_memory_timeline('s')) == 1
    assert migrate_legacy('s', apply=True) == []


def test_export_replacements_are_json_safe(tmp_path):
    issue = capture_issue('s', stage='write', reason='reported', snapshot={'text': '小明喜欢咖啡'})
    review_issue('s', issue['id'], status='confirmed', expected={'subject': '小明'}, reviewer='owner')
    path = tmp_path / 'cases.jsonl'
    export_cases('s', path, replacements={'小明': '人物"一"'})
    assert json.loads(path.read_text())['expected']['subject'] == '人物"一"'


def test_related_card_survives_final_evidence_filter():
    from agent.ask_executor import _record_relevant_to_unit
    from agent.ask_models import QueryUnit
    write('我喜欢咖啡')
    write('我喜欢拿铁', 'n2')
    hits = memory_search('s', '我喜欢咖啡吗', memory_type='preference')
    unit = QueryUnit(id='u1', question='我喜欢咖啡吗', intent='preference_current', topic='咖啡')
    assert all(_record_relevant_to_unit(unit, hit) for hit in hits)
    assert len(hits) == 2


def test_digest_only_reports_new_issues_and_unroutable_does_not_block(monkeypatch):
    from memory.runtime_workflow import daily_issue_digest, register_route, flush_notifications
    sent = []
    monkeypatch.setattr('bot.feishu_bot.send_text', lambda *args: sent.append(args))
    create_pending('s', note('缺少路由的消息', 'missing'), '等待修复')
    register_route('s', 'n1', chat_id='test-chat')
    create_pending('s', note('他喜欢咖啡'), '主体不明确')
    daily_issue_digest('s', day='2026-10-18')
    daily_issue_digest('s', day='2026-10-19')
    notifications = state.list_items('s', 'notification')
    assert len([n for n in notifications if n['id'].startswith('digest:')]) == 1
    assert flush_notifications('s') == 2
    assert len(sent) == 2


def test_forgotten_evidence_is_not_sent_to_decision_model(monkeypatch):
    write('我喜欢低糖咖啡')
    write('请忘记我喜欢低糖咖啡', 'forget')
    current = cards.find_card('s', '用户', '咖啡')
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'llm')
    seen = []
    def decision_model(**kwargs):
        seen.append(json.loads(kwargs['user_prompt']))
        return {'action': 'append', 'reason': '新的偏好'}
    monkeypatch.setattr(cards, 'complete_json', decision_model)
    detail = cards.DetailInput(subject='用户', preference_object='咖啡', attitude='喜欢',
                               description='我喜欢咖啡', evidence_span='我喜欢咖啡')
    cards.decide(detail, current.scope['preference_card'], [current], {'note_id': 'new'})
    assert '低糖' not in json.dumps(seen, ensure_ascii=False)


def test_general_attitude_for_named_person_and_dislike():
    write('我的同事小明喜欢咖啡')
    write('我讨厌咖啡', 'n2')
    assert card(subject='同事小明')['attitude'] == '喜欢'
    assert card()['attitude'] == '不喜欢'


def test_forget_specific_variant_keeps_other_variant():
    write('我喜欢低糖咖啡')
    write('我喜欢高糖咖啡', 'n2')
    report = write('请忘记我喜欢低糖咖啡', 'forget')
    assert report['results'][0]['action'] == 'forget'
    active = cards.active_details(card())
    assert len(active) == 1 and '高糖' in active[0]['description']


@pytest.mark.parametrize('memory_type', ['preference', 'task', 'semantic', 'episodic'])
def test_unreliable_extraction_of_any_type_is_pending(monkeypatch, memory_type):
    candidate = MemoryCandidate(memory_type, '用户喜欢咖啡', .8, .1, evidence_span='我喜欢咖啡')
    monkeypatch.setattr('memory.service.extract_candidates', lambda *args, **kwargs: [candidate])
    write('我喜欢咖啡')
    pending = state.list_items('s', 'pending')
    assert len(pending) == 1 and pending[0]['code'] == 'confidence_below_minimum'
    assert '我喜欢咖啡' in state.list_items('s', 'notification')[0]['text']


def test_console_pending_delivered_to_originating_trace(monkeypatch):
    from unittest.mock import Mock
    from memory.runtime_workflow import register_route, flush_notifications
    from repositories.postgres import lifecycle, dispatch
    save, enqueue = Mock(), Mock()
    monkeypatch.setattr(lifecycle, 'update_run', save)
    monkeypatch.setattr(dispatch, 'enqueue_task', enqueue)
    register_route('s', 'n1', chat_id='console:trace-test', trace_id='trace-test')
    create_pending('s', note('他喜欢咖啡'), '主体不明确')
    flush_notifications('s')
    assert save.call_args.args == ('trace-test',)
    assert '他喜欢咖啡' in save.call_args.kwargs['answer']
    assert '主体不明确' in save.call_args.kwargs['answer']
    enqueue.assert_not_called()


def test_legacy_candidate_does_not_trigger_second_extraction(monkeypatch):
    candidate = MemoryCandidate('preference', '用户喜欢电影', .8, .9,
        evidence_span='我喜欢他的电影《十二生肖》', scope={'reference_status': 'unresolved'})
    monkeypatch.setattr('memory.service.extract_candidates', lambda *a, **kw: [candidate])
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'llm')
    def unexpected_model(**kwargs):
        pytest.fail('card writes must not re-extract preferences')
    monkeypatch.setattr(cards, 'complete_json', unexpected_model)
    report = write('我喜欢他的电影《十二生肖》', previous_messages=[
        {'note_id': 'actor', 'text': '我好喜欢成龙大哥啊。'}])
    assert report['results'][0]['action'] == 'pending_review'
    assert cards.find_card('s', '用户', '电影') is None
    assert '第一阶段' in state.list_items('s', 'pending')[0]['reason']


def test_object_reference_cannot_use_fabricated_antecedent(monkeypatch):
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'llm')
    candidate = MemoryCandidate('preference', '用户喜欢电影', .8, .9,
        evidence_span='我喜欢他的电影《十二生肖》', scope={'preference_detail': {
            'subject': '用户', 'preference_object': '电影', 'attitude': '喜欢',
            'description': '喜欢成龙的电影《十二生肖》', 'evidence_span': '我喜欢他的电影《十二生肖》',
            'antecedent_note_id': 'nonexistent'}})
    result = cards.apply_preference(candidate, note('我喜欢他的电影《十二生肖》'))
    assert result['action'] == 'pending_review'
    assert cards.find_card('s', '用户', '电影') is None


def test_general_attitude_does_not_depend_on_description_wording():
    value = {'subject': '用户', 'preference_object': '成龙', 'details': [
        {'attitude': '喜欢', 'condition': '', 'description': '用户表达了对成龙的强烈喜欢，称呼为成龙大哥。',
         'is_general': True, 'status': 'active'}]}
    assert cards.project_card(value)['attitude'] == '喜欢'
    value['preference_object'] = '电影'
    value['details'][0].update(description='喜欢成龙的电影《十二生肖》', is_general=False)
    assert cards.project_card(value)['attitude'] == '未说明'
