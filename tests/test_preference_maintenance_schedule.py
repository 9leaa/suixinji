from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from memory.preference_maintenance import cards_due, excluded

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
def card(changes=0, days=2, count=1):
    return SimpleNamespace(id='a', status='active', created_at=(NOW-timedelta(days=days)).isoformat(), scope={'preference_card':{'changes':changes,'last_consolidated_changes':0,'details':[{'status':'active'} for _ in range(count)]}})

def test_unchanged_card_not_due_after_one_day():
    assert not cards_due([card()], {}, NOW)

def test_changed_card_due_after_day():
    assert cards_due([card(changes=1)], {}, NOW)

def test_recent_change_waits_for_threshold():
    assert not cards_due([card(changes=1,days=0)], {}, NOW)

def test_change_threshold_runs_early():
    assert cards_due([card(changes=30,days=0)], {}, NOW)

def test_detail_threshold_runs_early():
    assert cards_due([card(changes=1,days=0,count=10)], {}, NOW)

def test_weekly_relationship_refresh():
    assert cards_due([card(days=7)], {}, NOW)

def test_finished_job_uses_last_run():
    assert not cards_due([card(changes=1)], {'a':{'last_run':NOW.isoformat(),'last_related_run':NOW.isoformat()}}, NOW)

def test_evaluation_spaces_excluded():
    assert excluded('sample30-20261018-1905-d02_c05')
    assert excluded('task-deploy-smoke-20261021')
    assert excluded('fmeval_test')
    assert not excluded('console')
