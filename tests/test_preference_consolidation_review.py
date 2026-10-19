from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from core import settings
from memory import preference_cards as cards, preference_consolidation as review
from memory import repository as repo, workflow_state as state
from memory.models import MemoryCandidate
from memory.preference_schema import DetailInput


@pytest.fixture
def notes(monkeypatch):
    values = {}
    monkeypatch.setattr(settings, 'PREFERENCE_CARDS_ENABLED', True)
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'rules')
    monkeypatch.setattr('storage.note_storage.find_note', lambda space, key: deepcopy(values.get(key)))
    return values


def seed(notes, target, text, *, attitude='喜欢', condition='', subject='用户', owner='owner', day=18):
    key = f'n{len(notes)}'
    note = {'id': key, 'space_id': 's', 'user_id': owner, 'text': text, 'ts': f'2026-10-{day}T10:00:00+08:00'}
    notes[key] = note
    detail = DetailInput(subject=subject, preference_object=target, attitude=attitude,
                         condition=condition, description=text, evidence_span=text, is_general=not bool(condition))
    cards.apply_preference(MemoryCandidate('preference', text, .8, .9, evidence_span=text), note,
                           prepared=detail, override_plan=cards.CardPlan())
    return cards.find_card('s', subject, target, owner)


def plan_for(root, action, *, ids=None, sources=None, **kwargs):
    details = cards.active_details(root.scope['preference_card'])
    return review.Action(action=action, card_id=root.id, detail_ids=ids or [details[0]['id']],
        source_ids=sources or [s['id'] for d in details for s in d['sources']], reason='根据原文核验', **kwargs)


def assessments(root):
    return [review.Assessment(detail_id=d['id'], verdict='supported',
        source_ids=[s['id'] for s in d['sources']], quote=d['sources'][0]['original_text'], reason='测试已确认原文')
        for d in cards.active_details(root.scope['preference_card'])]


def run_one(monkeypatch, root, actions=(), relations=(), now=None):
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'llm')
    monkeypatch.setattr(review, 'plan_review', lambda payload: review.Review(assessments=assessments(root), relations=list(relations), actions=list(actions)))
    return review.consolidate(root, job=state.get('s', 'card_job', root.id), now=now or datetime.now().astimezone())


def test_hybrid_candidates_survive_legacy_identity_zero(notes, monkeypatch):
    a = seed(notes, '教材', '晚上看不进教材', condition='晚上')
    b = seed(notes, '看教材', '早上看教材', condition='早上')
    stranger = seed(notes, '看教材', '同事看教材', owner='other')
    monkeypatch.setattr(repo, 'hybrid_adjudication_candidates', lambda *a, **k: [b, stranger])
    monkeypatch.setattr('memory.candidate_retriever.retrieve_candidates', lambda *a, **k: pytest.fail('legacy gate used'))
    data = DetailInput(subject='用户', preference_object='教材', attitude='不确定', description=a.content, evidence_span=a.content)
    assert [r.id for r in cards.retrieval('s', data)] == [a.id, b.id]


def test_single_direction_and_no_implicit_reverse(notes):
    coffee = seed(notes, '咖啡', '我喜欢咖啡')
    latte = seed(notes, '拿铁', '我喜欢拿铁')
    assert cards.find_card('s', '用户', '咖啡').scope['preference_card']['related_ids'] == []
    assert latte.scope['preference_card']['related_ids'] == [coffee.id]


def test_unsupported_attitude_is_pending_and_keeps_original(notes, monkeypatch):
    root = seed(notes, '视频课', '晚上听视频课比较能坚持', condition='晚上')
    previous_failure = review.create_pending('s', notes['n0'], '旧整理失败', code='consolidation_failed', target_ids=[root.id])
    run_one(monkeypatch, root, [plan_for(root, 'unsupported')])
    assert state.get('s', 'pending', previous_failure['id'])['status'] == 'resolved'
    current = repo.get_memory(root.id).scope['preference_card']
    assert cards.active_details(current) == []
    assert current['details'][0]['status'] == 'pending_review'
    assert current['details'][0]['sources'][0]['original_text'] == '晚上听视频课比较能坚持'
    assert state.list_items('s', 'pending')
    assert '晚上听视频课比较能坚持' in state.list_items('s', 'notification')[0]['text']
    assert state.list_items('s', 'issue')
    assert 'pending_review' in json.dumps(cards.evidence(repo.get_memory(root.id), include_history=True))


def test_explicit_change_preserves_history_and_other_condition(notes, monkeypatch):
    seed(notes, '咖啡', '早上不喜欢咖啡', attitude='不喜欢', condition='早上')
    seed(notes, '咖啡', '晚上喜欢咖啡', condition='晚上')
    root = seed(notes, '咖啡', '现在早上也喜欢咖啡了', condition='早上', day=19)
    details = root.scope['preference_card']['details']
    action = plan_for(root, 'change', ids=[details[0]['id'], details[2]['id']],
        sources=[details[2]['sources'][0]['id']], replacement=review.Replacement(
            attitude='喜欢', condition='早上', description='现在早上也喜欢咖啡了'))
    # The action must also cite the old assertion it changes.
    action.source_ids.append(details[0]['sources'][0]['id'])
    run_one(monkeypatch, root, [action])
    current = repo.get_memory(root.id).scope['preference_card']
    assert [(d['condition'], d['status']) for d in current['details']] == [('早上', 'history'), ('晚上', 'active'), ('早上', 'active')]


def test_general_and_exception_unchanged(notes, monkeypatch):
    seed(notes, '咖啡', '我喜欢咖啡')
    root = seed(notes, '咖啡', '早上不喜欢咖啡', attitude='不喜欢', condition='早上')
    run_one(monkeypatch, root)
    current = repo.get_memory(root.id).scope['preference_card']
    assert current['attitude'] == '喜欢'
    assert len(cards.active_details(current)) == 2


def test_duplicate_combines_sources_not_conditions(notes, monkeypatch):
    seed(notes, '咖啡', '我喜欢咖啡')
    root = seed(notes, '咖啡', '我爱喝咖啡')
    action = plan_for(root, 'deduplicate', ids=[d['id'] for d in root.scope['preference_card']['details']])
    run_one(monkeypatch, root, [action])
    details = cards.active_details(repo.get_memory(root.id).scope['preference_card'])
    assert len(details) == 1 and len(details[0]['sources']) == 2


def test_equivalent_cards_merge_atomically_and_redirect_alias(notes, monkeypatch):
    target = seed(notes, '看教材', '我喜欢看教材')
    root = seed(notes, '教材', '我喜欢阅读教材')
    incoming = seed(notes, '学习', '我喜欢学习')
    incoming_card = deepcopy(incoming.scope['preference_card'])
    incoming_card['related_ids'] = [root.id]
    cards._persist('s', incoming_card, incoming, {'note_id': 'n2'}, 'incoming', reason='test')
    source_ids = [d['sources'][0]['id'] for r in [root, target] for d in r.scope['preference_card']['details']]
    action = plan_for(root, 'merge_cards', target_card_id=target.id, sources=source_ids)
    monkeypatch.setattr(cards, 'retrieval', lambda *a, **k: [target])
    run_one(monkeypatch, root, [action], [review.Relation(card_id=target.id, relation='equivalent', reason='原文均指阅读教材')])
    assert repo.get_memory(root.id).status == 'archived'
    assert cards.find_card('s', '用户', '教材').id == target.id
    assert len(cards.active_details(repo.get_memory(target.id).scope['preference_card'])) == 2
    assert repo.get_memory(incoming.id).scope['preference_card']['related_ids'] == [target.id]
    assert incoming.id not in repo.get_memory(target.id).scope['preference_card']['related_ids']


def test_subtype_is_not_merge_permission(notes, monkeypatch):
    target = seed(notes, '咖啡', '我喜欢咖啡')
    root = seed(notes, '拿铁', '我喜欢拿铁')
    monkeypatch.setattr(cards, 'retrieval', lambda *a, **k: [target])
    with pytest.raises(ValueError, match='同义'):
        run_one(monkeypatch, root, [plan_for(root, 'merge_cards', target_card_id=target.id)],
            [review.Relation(card_id=target.id, relation='related', reason='上下位')])
    assert repo.get_memory(root.id).current_version == root.current_version
    assert repo.get_memory(target.id).current_version == target.current_version


def test_source_deleted_does_not_recreate_preference(notes, monkeypatch):
    root = seed(notes, '咖啡', '我喜欢咖啡')
    notes.clear()
    monkeypatch.setattr(review, 'plan_review', lambda p: pytest.fail('deleted evidence reached model'))
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'llm')
    result = review.consolidate(root, job=state.get('s', 'card_job', root.id), now=datetime.now().astimezone())
    assert result['action'] == 'consolidated'
    assert cards.active_details(repo.get_memory(root.id).scope['preference_card']) == []


def test_concurrent_note_edit_rolls_back(notes, monkeypatch):
    root = seed(notes, '咖啡', '我喜欢咖啡')
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'llm')
    def plan(payload):
        notes['n0']['text'] = '修改后的原文'
        return review.Review(assessments=assessments(root), relations=[], actions=[])
    monkeypatch.setattr(review, 'plan_review', plan)
    with pytest.raises(cards.CardVersionConflict):
        review.consolidate(root, job=state.get('s', 'card_job', root.id), now=datetime.now().astimezone())
    assert repo.get_memory(root.id).current_version == root.current_version
    assert state.list_items('s', 'consolidation_run') == []


def test_concurrent_card_update_rolls_back_all(notes, monkeypatch):
    root = seed(notes, '咖啡', '我喜欢咖啡')
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'llm')
    def plan(payload):
        cards._persist('s', deepcopy(root.scope['preference_card']), root, {'note_id': 'n0'}, 'concurrent', reason='test')
        return review.Review(assessments=assessments(root), relations=[], actions=[])
    monkeypatch.setattr(review, 'plan_review', plan)
    with pytest.raises(cards.CardVersionConflict):
        review.consolidate(root, job=state.get('s', 'card_job', root.id), now=datetime.now().astimezone())
    assert state.list_items('s', 'consolidation_run') == []


def test_clean_card_refreshes_after_seven_days(notes, monkeypatch):
    root = seed(notes, '咖啡', '我喜欢咖啡')
    now = datetime.now().astimezone()
    cards.run_consolidation('s', force=True, now=now)
    job = state.get('s', 'card_job', root.id)
    assert not job['dirty']
    assert cards.run_consolidation('s', now=now + timedelta(days=8))


def test_unknown_source_or_action_cannot_mutate(notes, monkeypatch):
    root = seed(notes, '咖啡', '我喜欢咖啡')
    with pytest.raises(ValueError, match='来源'):
        run_one(monkeypatch, root, [plan_for(root, 'unsupported', sources=['invented'])])
    assert repo.get_memory(root.id).current_version == root.current_version


def test_missing_relation_cannot_mutate(notes, monkeypatch):
    first = seed(notes, '咖啡', '我喜欢咖啡')
    root = seed(notes, '拿铁', '我喜欢拿铁')
    monkeypatch.setattr(cards, 'retrieval', lambda *a, **k: [first])
    with pytest.raises(ValueError, match='覆盖候选'):
        run_one(monkeypatch, root)


def test_related_subjects_never_merge(notes, monkeypatch):
    other = seed(notes, '教材', '同事喜欢看教材', subject='同事小明')
    root = seed(notes, '看教材', '我喜欢看教材')
    monkeypatch.setattr(cards, 'retrieval', lambda *a, **k: [other])
    with pytest.raises(ValueError, match='跨主体'):
        run_one(monkeypatch, root, [plan_for(root, 'merge_cards', target_card_id=other.id)],
                [review.Relation(card_id=other.id, relation='equivalent', reason='同义')])


def test_conflict_requires_confirmation_both_sources_retained(notes, monkeypatch):
    seed(notes, '咖啡', '我喜欢咖啡')
    root = seed(notes, '咖啡', '我不喜欢咖啡', attitude='不喜欢', day=19)
    action = plan_for(root, 'pending', ids=[d['id'] for d in root.scope['preference_card']['details']])
    run_one(monkeypatch, root, [action])
    current = repo.get_memory(root.id)
    assert not cards.active_details(current.scope['preference_card'])
    assert len(cards.evidence(current, include_history=True)['sources']) == 2
    assert current.scope['preference_card']['attitude'] == '未说明'


def test_old_fact_cannot_update_new_state_even_if_old_target_is_cited(notes, monkeypatch):
    seed(notes, '咖啡', '现在不喜欢咖啡', attitude='不喜欢', day=19)
    root = seed(notes, '咖啡', '之前喜欢咖啡', day=18)
    details = root.scope['preference_card']['details']
    action = plan_for(root, 'change', ids=[d['id'] for d in details],
                      replacement=review.Replacement(attitude='喜欢', condition='', description='喜欢咖啡'))
    with pytest.raises(ValueError, match='更早'):
        run_one(monkeypatch, root, [action])


def test_explicit_correction_can_change_condition(notes, monkeypatch):
    root = seed(notes, '咖啡', '早上喜欢咖啡', condition='早上')
    correction = {'id': 'fix', 'space_id': 's', 'text': '刚才说错了，是晚上喜欢咖啡', 'ts': '2026-10-19T10:00:00+08:00'}
    notes['fix'] = correction
    source = {'id': 'fix-source', 'note_id': 'fix', 'original_text': correction['text'], 'message_time': correction['ts']}
    payload, available = review.build_evidence('s', {root.id: root}, root)
    available[source['id']] = review.read_source('s', source)
    action = plan_for(root, 'correct', sources=[next(iter(available)), source['id']],
                      replacement=review.Replacement(attitude='喜欢', condition='晚上', description='晚上喜欢咖啡'))
    changes, _ = review.apply_plan(root, {root.id: root}, review.Review(assessments=assessments(root), relations=[], actions=[action]), available)
    assert [(d['condition'], d['status']) for d in changes[root.id]['details']] == [('早上', 'history'), ('晚上', 'active')]


def test_forget_cleans_incoming_links(notes):
    coffee = seed(notes, '咖啡', '我喜欢咖啡')
    latte = seed(notes, '拿铁', '我喜欢拿铁')
    assert coffee.id in latte.scope['preference_card']['related_ids']
    cards.forget('s', {'id': 'forget', 'space_id': 's', 'text': '请忘记我所有咖啡偏好'})
    assert repo.get_memory(latte.id).scope['preference_card']['related_ids'] == []


def test_console_manual_consolidation_uses_traced_preference_path(monkeypatch):
    from apps.console_commands import execute_command
    calls = []
    monkeypatch.setattr(cards, 'run_consolidation', lambda space, **kw: calls.append((space, kw)) or [{'action': 'consolidated'}])
    assert 'consolidated' in execute_command('s', '/memory consolidate preferences')
    assert calls == [('s', {'force': True})]


def test_independent_evidence_check_overrides_unsupported_attitude(notes, monkeypatch):
    root = seed(notes, '教材', '晚上看不进教材', attitude='不喜欢', condition='晚上')
    payload, available = review.build_evidence('s', {root.id: root}, root)
    calls = []
    def model(**kwargs):
        calls.append(kwargs['system_prompt'])
        if len(calls) == 1:
            return {'assessments': [a.model_dump() for a in assessments(root)], 'relations': [], 'actions': []}
        return {'assessments': [{**assessments(root)[0].model_dump(), 'verdict': 'unsupported',
                                 'quote': '', 'reason': '能力困难不代表不喜欢'}]}
    monkeypatch.setattr(review, 'complete_json', model)
    plan = review.plan_review(payload)
    changes, pending = review.apply_plan(root, {root.id: root}, plan, available)
    assert len(calls) == 2
    assert len(pending) == 1
    assert changes[root.id]['details'][0]['status'] == 'pending_review'


def test_attitude_review_cannot_omit_details(notes):
    root = seed(notes, '咖啡', '我喜欢咖啡')
    _, available = review.build_evidence('s', {root.id: root}, root)
    with pytest.raises(ValueError, match='完整覆盖'):
        review.apply_plan(root, {root.id: root}, review.Review(assessments=[], relations=[], actions=[]), available)


def test_model_cannot_review_neighbor_detail_id(notes, monkeypatch):
    root = seed(notes, '咖啡', '我喜欢咖啡')
    payload, _ = review.build_evidence('s', {root.id: root}, root)
    def model(**kwargs):
        sent = json.loads(kwargs['user_prompt'])
        assert sent['root_card']['id'] == root.id
        assert sent['reference_cards'] == []
        value = assessments(root)[0].model_dump()
        value['detail_id'] = 'neighbor-detail'
        return {'assessments': [value], 'relations': [], 'actions': []}
    monkeypatch.setattr(review, 'complete_json', model)
    with pytest.raises(ValueError, match='参考卡片'):
        review.plan_review(payload)
