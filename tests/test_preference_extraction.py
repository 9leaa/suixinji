"""First-stage preference contract through real validation and card storage."""
import json
from unittest.mock import Mock

import pytest

from core import settings
from memory import extractor, preference_cards as cards, workflow_state as state
from memory.candidate_validator import validate_candidate
from memory.service import process_note_memory


@pytest.fixture(autouse=True)
def direct_mode(monkeypatch):
    monkeypatch.setattr(settings, 'PREFERENCE_CARDS_ENABLED', True)
    monkeypatch.setattr(settings, 'MEMORY_EXTRACTOR_MODE', 'hybrid')
    monkeypatch.setattr(extractor, 'MEMORY_EXTRACTOR_MODE', 'hybrid')
    monkeypatch.setattr(extractor, '_LLM_CONNECTION_CIRCUIT_OPEN_UNTIL', 0)
    # A newly created card has no merge decision to make.
    monkeypatch.setattr(cards, 'complete_json', Mock(side_effect=AssertionError('unexpected second extraction')))


def row(text='我喜欢咖啡', **changes):
    return {'memory_type': 'preference', 'subject': '用户', 'preference_object': '咖啡',
            'attitude': '喜欢', 'condition': '', 'description': '喜欢咖啡', 'is_general': True,
            'evidence_span': text, 'confidence': .95, 'importance': .7, **changes}


def write(monkeypatch, text, rows, previous=None, key='new'):
    model = Mock(return_value={'candidates': rows})
    monkeypatch.setattr(extractor, 'complete_json', model)
    report = process_note_memory({'id': key, 'space_id': 's', 'text': text,
                                 'ts': '2026-10-18T18:00:00+08:00', 'previous_messages': previous or []})
    assert model.call_count == 1
    return report, model


def test_new_contract_is_stored_without_second_extraction(monkeypatch):
    report, model = write(monkeypatch, '我喜欢咖啡', [row()])
    assert [r['action'] for r in report['results']] == ['insert']
    final = report['results'][0]['preference_card']
    assert (final['subject'], final['preference_object'], final['attitude']) == ('用户', '咖啡', '喜欢')
    assert final['details'][0]['sources'][0]['message_time'] == '2026-10-18T18:00:00+08:00'
    assert 'preference_object' in model.call_args.kwargs['system_prompt']
    cards.complete_json.assert_not_called()


@pytest.mark.parametrize('antecedent', ['actor', None, 'forged'])
def test_object_reference_uses_first_stage_result_and_checks_source(monkeypatch, antecedent):
    text = '我喜欢他的电影《十二生肖》'
    detail = row(text, preference_object='电影', description='喜欢成龙的电影《十二生肖》',
                 is_general=False, antecedent_note_id=antecedent)
    report, model = write(monkeypatch, text, [detail], [{'note_id': 'actor', 'text': '我好喜欢成龙大哥啊。'}])
    if antecedent == 'actor':
        final = report['results'][0]['preference_card']
        assert (final['subject'], final['preference_object'], final['attitude']) == ('用户', '电影', '未说明')
        assert final['details'][0]['sources'][0]['antecedent_note_id'] == 'actor'
    else:
        assert report['results'][0]['action'] == 'pending_review'
        assert cards.find_card('s', '用户', '电影') is None
    assert json.loads(model.call_args.kwargs['user_prompt'])['previous_messages'][0]['note_id'] == 'actor'
    cards.complete_json.assert_not_called()


def test_subject_reference_preserves_title(monkeypatch):
    text = '他喜欢喝咖啡'
    report, _ = write(monkeypatch, text, [row(text, subject='同事小明', description='同事小明喜欢咖啡',
        antecedent_note_id='person')], [{'note_id': 'person', 'text': '我的同事小明今天来了'}])
    assert report['results'][0]['preference_card']['subject'] == '同事小明'
    assert cards.find_card('s', '用户', '咖啡') is None


def test_inline_person_reference_does_not_require_previous_message(monkeypatch):
    text = '同事小林恰恰相反，特别喜欢线下开会，人多他更有精神。'
    report, _ = write(monkeypatch, text, [row(text, subject='同事小林', preference_object='开会',
        condition='线下', description='同事小林喜欢线下开会，人多更有精神', is_general=False,
        local_antecedent='同事小林')])
    final = report['results'][0]['preference_card']
    assert final['subject'] == '同事小林'
    assert final['details'][0]['sources'][0]['local_antecedent'] == '同事小林'
    assert cards.find_card('s', '用户', '开会') is None


def test_inline_object_reference_preserves_user_subject(monkeypatch):
    text = '同事小林推荐咖啡，我喜欢他推荐的咖啡'
    report, _ = write(monkeypatch, text, [row('我喜欢他推荐的咖啡',
        description='喜欢同事小林推荐的咖啡', local_antecedent='同事小林', is_general=False)])
    assert report['results'][0]['preference_card']['subject'] == '用户'


@pytest.mark.parametrize('changes', [
    {'local_antecedent': '同事小王'},
    {'local_antecedent': '他'},
    {'local_antecedent': '同事小林', 'description': '喜欢咖啡', 'subject': '用户'},
    {'local_antecedent': '同事小林', 'antecedent_note_id': 'forged'},
    {'local_antecedent': '同事小林', 'unresolved_reason': '两人都可能是先行词'},
])
def test_invalid_inline_reference_is_pending(monkeypatch, changes):
    text = '同事小林说他喜欢咖啡'
    detail = row(text, subject='同事小林', description='同事小林喜欢咖啡')
    detail.update(changes)
    report, _ = write(monkeypatch, text, [detail])
    assert report['results'][0]['action'] == 'pending_review'


@pytest.mark.parametrize('changes', [
    {'unresolved_reason': '前文有两个人，无法确定他指的是谁'},
    {'attitude': 'maybe'}, {'is_general': None}, {'preference_object': ''},
])
def test_uncertain_or_invalid_rows_are_pending_and_notify(monkeypatch, changes):
    report, _ = write(monkeypatch, '我喜欢咖啡', [row(**changes)])
    assert report['results'][0]['action'] == 'pending_review'
    assert cards.find_card('s', '用户', '咖啡') is None
    assert '我喜欢咖啡' in state.list_items('s', 'notification')[0]['text']
    cards.complete_json.assert_not_called()


def test_neutral_attitude_is_not_rewritten_by_legacy_rules(monkeypatch):
    text = '我对咖啡无所谓'
    report, _ = write(monkeypatch, text, [row(text, attitude='无所谓', description='对咖啡无所谓')])
    assert report['results'][0]['preference_card']['attitude'] == '无所谓'


def test_conditions_and_variants_are_not_generalized(monkeypatch):
    text = '我早上不喜欢低糖咖啡'
    report, _ = write(monkeypatch, text, [row(text, attitude='不喜欢', condition='早上',
        description='早上不喜欢低糖咖啡', is_general=False)])
    final = report['results'][0]['preference_card']
    assert final['preference_object'] == '咖啡' and final['attitude'] == '未说明'
    assert final['details'][0]['condition'] == '早上'
    assert '低糖' in final['details'][0]['description']


def test_mixed_schema_preserves_other_types(monkeypatch):
    text = '我喜欢咖啡；记得写测试报告；我住在北京；昨天参加了发布会'
    rows = [row('我喜欢咖啡'),
        {'memory_type': 'task', 'entity': '用户', 'attribute': '测试报告', 'operation': '撰写',
         'canonical_topic': '撰写测试报告', 'task_status': 'todo', 'content': '用户要写测试报告', 'evidence_span': '记得写测试报告'},
        {'memory_type': 'semantic', 'entity': '用户', 'attribute': 'location', 'new_value': '北京',
         'content': '用户住在北京', 'evidence_span': '我住在北京'},
        {'memory_type': 'episodic', 'entity': '用户', 'attribute': 'event', 'canonical_topic': '参加发布会',
         'content': '用户昨天参加了发布会', 'evidence_span': '昨天参加了发布会'}]
    monkeypatch.setattr(extractor, 'complete_json', lambda **kw: {'candidates': rows})
    candidates = extractor.extract_candidates('mixed', text)
    assert {c.memory_type for c in candidates} == {'preference', 'task', 'semantic', 'episodic'}
    assert len([c for c in candidates if c.memory_type == 'preference']) == 1
    for candidate in candidates:
        checked, rejection = validate_candidate(candidate, note_text=text)
        assert checked is not None, rejection


def test_uncovered_second_preference_is_pending_without_second_call(monkeypatch):
    text = '我喜欢咖啡和绿茶'
    report, _ = write(monkeypatch, text, [row(text)])
    assert sorted(r['action'] for r in report['results']) == ['insert', 'pending_review']
    assert cards.find_card('s', '用户', '绿茶') is None


def test_model_failure_does_not_trigger_hidden_reextraction(monkeypatch):
    model = Mock(side_effect=RuntimeError('provider unavailable'))
    monkeypatch.setattr(extractor, 'complete_json', model)
    report = process_note_memory({'id': 'failure', 'space_id': 's', 'text': '我喜欢咖啡'})
    assert report['results'][0]['action'] == 'pending_review'
    assert model.call_count == 1
    cards.complete_json.assert_not_called()


def test_unresolved_should_not_store_still_notifies(monkeypatch):
    report, _ = write(monkeypatch, '他喜欢咖啡', [row('他喜欢咖啡', should_store=False,
        confidence=.5, unresolved_reason='没有前文，无法判断他是谁')])
    assert report['rejected_candidates'] == 1
    assert cards.find_card('s', '用户', '咖啡') is None
    pending = state.list_items('s', 'pending')
    assert pending[0]['reason'] == '没有前文，无法判断他是谁'
    notification = state.list_items('s', 'notification')[0]['text']
    assert '他喜欢咖啡' in notification and '无法判断他是谁' in notification
