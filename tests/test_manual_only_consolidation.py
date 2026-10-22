from core import settings
from memory import fact_memory, preference_cards, preference_maintenance, scheduler


def test_automatic_entries_skip_before_reading_storage(monkeypatch):
    monkeypatch.setattr(settings, 'MEMORY_AUTO_CONSOLIDATION_ENABLED', False)
    assert fact_memory.enqueue_due('s') == []
    assert fact_memory.consolidate('s', 'semantic', trigger='threshold')['status'] == 'skipped'
    assert preference_maintenance.enqueue_due('s') is None
    assert preference_cards.run_consolidation('s')['status'] == 'skipped'
    monkeypatch.setattr(scheduler, 'flush_access_counts', lambda: 0)
    assert scheduler.run_memory_scheduler_tick()['ran'] == []


def test_old_automatic_jobs_do_not_execute(monkeypatch):
    from apps.handlers import handle_memory
    monkeypatch.setattr(settings, 'MEMORY_AUTO_CONSOLIDATION_ENABLED', False)
    for operation in ('consolidate', 'fact_consolidate', 'preference_maintenance'):
        assert handle_memory({'space_id': 's', 'payload_json': {'operation': operation}}) is None


def test_manual_fact_request_still_reaches_consolidator(monkeypatch):
    from apps.handlers import handle_memory
    monkeypatch.setattr(settings, 'MEMORY_AUTO_CONSOLIDATION_ENABLED', False)
    monkeypatch.setattr(settings, 'FACT_MEMORY_ENABLED', True)
    calls = []
    monkeypatch.setattr(fact_memory, 'consolidate', lambda *args, **kw: calls.append(kw) or {'status': 'completed'})
    handle_memory({'space_id': 's', 'payload_json': {'operation': 'fact_consolidate', 'memory_type': 'semantic', 'trigger': 'manual'}})
    assert calls == [{'trigger': 'manual'}]
