import json
from unittest.mock import Mock

from agent import query_agent
from agent.hooks.context import AgentRunContext
from core import answer_grounding as grounding
from memory import semantic_profile_projection as projection


def test_identity_is_loaded_once_from_account_not_question(monkeypatch):
    lookup = Mock(return_value={'id': 'u1', 'name': '李明', 'name_source': 'account_profile'})
    monkeypatch.setattr(grounding, 'current_user_identity', lookup)
    context = AgentRunContext.create(space_id='s', run_type='query', user_id='u1', tenant_id='t1')
    model = Mock(return_value={'final_answer': '没有小林的明确偏好记录'})
    monkeypatch.setattr(query_agent, '_complete_json_with_hooks', model)
    for _ in range(2):
        query_agent._synthesize_answer('小林喜欢什么？', [], hook_context=context)
    payload = json.loads(model.call_args.kwargs['user_prompt'])
    assert payload['current_user']['name'] == '李明'
    lookup.assert_called_once_with('u1', tenant_id='t1')
    assert '禁止写“小林（记录中的用户）”' in model.call_args.kwargs['system_prompt']


def test_unknown_identity_stays_unknown():
    assert grounding.current_user_identity('u1')['name'] is None
    assert grounding.identity_for_run(None)['name'] is None


def test_all_generation_prompts_share_identity_and_evidence_rules():
    from agent.ask_workflow import ASK_ANSWER_PROMPT, ASK_ANSWER_CONTRACT_REPAIR_PROMPT
    for prompt in (query_agent.REACT_SYSTEM_PROMPT, query_agent.FINAL_SYSTEM_PROMPT,
                   ASK_ANSWER_PROMPT, ASK_ANSWER_CONTRACT_REPAIR_PROMPT,
                   projection._FACET_PROJECTION_PROMPT, projection._FINAL_PROFILE_PROMPT):
        assert grounding.IDENTITY_EVIDENCE_RULES in prompt


def test_profile_evidence_preserves_other_person_identity():
    from types import SimpleNamespace
    memory = SimpleNamespace(id='m1', subject='同事小林', scope={'owner_id': 'u1'},
        predicate='other', updated_at='2026-10-19', content='同事小林在线下开会', sources=[])
    payload = projection._memory_payload(memory)
    assert payload['subject'] == '同事小林'
    assert payload['owner_id'] == 'u1'
