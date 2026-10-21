"""Run only against an isolated preference_test* database; optional real model review."""
import argparse
from copy import deepcopy
from datetime import datetime
import json
import uuid

from core import settings
from memory import preference_cards as cards, preference_consolidation as review, repository as repo, workflow_state as state
from memory.models import MemoryCandidate
from memory.preference_schema import DetailInput
from repositories.postgres.notes import save_note


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    from sqlalchemy.engine import make_url
    url = make_url(settings.DATABASE_URL)
    if url.host not in {'127.0.0.1', 'localhost'} or not (url.database or '').startswith('preference_test'):
        raise RuntimeError('Requires an isolated local preference_test* database')
    assert settings.STORAGE_BACKEND == 'postgres'
    settings.PREFERENCE_CARDS_ENABLED = True
    settings.MEMORY_EXTRACTOR_MODE = 'rules'
    space = 'eval-preference-review-' + uuid.uuid4().hex[:10]
    sequence = 0
    def seed(target, text, attitude='喜欢', condition='', subject='用户'):
        nonlocal sequence
        sequence += 1
        note = {'id': f'{space}-{sequence}', 'message_id': f'{space}-{sequence}', 'space_id': space,
                'text': text, 'ts': f'2026-10-18T10:{sequence:02d}:00+08:00', 'title': '整理回归',
                'summary': '', 'type': '生活', 'tags': [], 'related': []}
        save_note(note)
        detail = DetailInput(subject=subject, preference_object=target, attitude=attitude,
                             condition=condition, description=text, evidence_span=text, is_general=not bool(condition))
        cards.apply_preference(MemoryCandidate('preference', text, .8, .9, evidence_span=text), note,
                              prepared=detail, override_plan=cards.CardPlan())
        return cards.find_card(space, subject, target)
    target = seed('看教材', '我喜欢看教材')
    root = seed('教材', '我喜欢阅读教材')
    job = state.get(space, 'card_job', root.id)
    original_plan = review.plan_review
    original_retrieval = cards.retrieval
    action = review.Action(action='merge_cards', card_id=root.id, target_card_id=target.id,
        source_ids=[d['sources'][0]['id'] for r in [root, target] for d in r.scope['preference_card']['details']], reason='相同阅读对象')
    review.plan_review = lambda _: review.Review(assessments=[review.Assessment(detail_id=d['id'], verdict='supported', source_ids=[d['sources'][0]['id']], quote=d['sources'][0]['original_text'], reason='原文明确') for d in cards.active_details(root.scope['preference_card'])], relations=[review.Relation(card_id=target.id, relation='equivalent', reason='均指阅读教材')], actions=[action])
    cards.retrieval = lambda *a, **k: [target]
    review.consolidate(root, job=job, now=datetime.now().astimezone())
    assert repo.get_memory(root.id).status == 'archived'
    assert cards.find_card(space, '用户', '教材').id == target.id
    assert len(cards.active_details(repo.get_memory(target.id).scope['preference_card'])) == 2
    # Optimistic multi-card transaction rejects stale snapshots.
    try:
        review.consolidate(root, job=job, now=datetime.now().astimezone())
    except cards.CardVersionConflict:
        pass
    else:
        raise AssertionError('stale root was accepted')
    review.plan_review, cards.retrieval = original_plan, original_retrieval
    results = {'space_id': space, 'postgres_checks': ['atomic_merge', 'alias_redirect', 'source_preservation', 'stale_snapshot_rejected']}
    if args.live:
        space = 'eval-preference-study-' + uuid.uuid4().hex[:10]
        seed('看教材', '为了六月的考试开始啃教材，早上脑子清楚，我一般都是早上看教材。', condition='早上')
        seed('教材', '晚上下班回来实在看不进教材，还是听视频课比较能坚持，晚上我听视频课。', attitude='不喜欢', condition='晚上下班回来')
        seed('视频课', '晚上下班回来实在看不进教材，还是听视频课比较能坚持，晚上我听视频课。', condition='晚上下班回来')
        seed('晚上看书', '晚上看书也没那么难了，最近晚上我也开始看教材，不只听视频了。', condition='晚上')
        seed('看书', '同事马萧说他从不看视频课，全靠自己啃书，说视频太慢。', subject='同事马萧')
        settings.MEMORY_EXTRACTOR_MODE = 'llm'
        report = cards.run_consolidation(space, force=True)
        cards_after = [{ 'id': r.id, 'subject': r.subject, 'object': r.object_value,
                        'status': r.status, 'card': deepcopy(r.scope['preference_card'])} for r in cards.all_cards(space)]
        results['live'] = {'space_id': space, 'runs': report, 'cards': cards_after,
                           'pending_count': len(state.list_items(space, 'pending'))}
        results['live']['all_behavioral_preferences_retained'] = all(
            d['status'] == 'active' for c in cards_after for d in c['card']['details'])
        space = 'eval-preference-positive-' + uuid.uuid4().hex[:10]
        settings.MEMORY_EXTRACTOR_MODE = 'rules'
        seed('咖啡', '我喜欢喝咖啡')
        seed('咖啡', '我不喜欢在早上喝咖啡', attitude='不喜欢', condition='早上')
        seed('咖啡', '我喜欢在晚上喝咖啡', condition='晚上')
        settings.MEMORY_EXTRACTOR_MODE = 'llm'
        positive_runs = cards.run_consolidation(space, force=True)
        positive_card = cards.find_card(space, '用户', '咖啡').scope['preference_card']
        results['positive_control'] = {'space_id': space, 'runs': positive_runs,
            'preserved': len(cards.active_details(positive_card)) == 3 and positive_card['attitude'] == '喜欢'}
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
