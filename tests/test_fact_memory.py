from datetime import datetime, timezone
import pytest

from core import settings
from memory import fact_memory as fm, repository


@pytest.fixture(autouse=True)
def enable(monkeypatch):
    monkeypatch.setattr(settings, "FACT_MEMORY_ENABLED", True)


def fact(text="用户住北京", kind="semantic", note="n1", space="s", **kwargs):
    return fm.ingest_fact(space, note_id=note, original_text=text, content=text,
                          memory_type=kind, enqueue=False, **kwargs)


def create(f, **kwargs):
    return {"action": "create", "source_fact_ids": [f["id"]], "reason": "明确原文支持",
            "content": f["content"], "memory_type": f["memory_type"], "assertion_kind": "stated", **kwargs}


def run(actions, kind="semantic", space="s"):
    return fm.consolidate(space, kind, planner=lambda *args: {"actions": actions})


def test_immediate_facts_do_not_write_legacy_memories():
    f = fact()
    assert not repository.list_memories("s")
    assert fm.query_hits("s", "北京")[0]["id"] == f["id"]
    assert fm.due("s", now=datetime(2026, 10, 20, 17, tzinfo=timezone.utc)) == []


def test_missing_proposal_feedback_identifies_uncovered_evidence():
    from memory.fact_agent import proposal_errors
    first, second = fact(), fact("当天到场人数为21人", note="n2")
    errors = proposal_errors([create(first)], [first, second],
                             {f["id"]: f for f in [first, second]})
    assert len(errors) == 1
    assert second["id"] in errors[0] and second["content"] in errors[0]
    assert first["id"] not in errors[0]


def test_threshold_counts_each_type_and_retries_are_idempotent():
    for i in range(29):
        fact(f"事件{i}", "episodic", f"e{i}")
    fact("另一条语义")
    assert fm.due("s", now=datetime(2026, 10, 20, 17, tzinfo=timezone.utc)) == []
    fact("事件29", "episodic", "e29")
    fact("事件29", "episodic", "e29")
    assert fm.due("s", now=datetime(2026, 10, 20, 17, tzinfo=timezone.utc)) == [("episodic", "threshold")]


def test_daily_uses_shanghai_and_skips_empty():
    assert not fm.due("s")
    fact()
    assert not fm.due("s", now=datetime(2026, 10, 19, 18, 59, tzinfo=timezone.utc))
    assert fm.due("s", now=datetime(2026, 10, 19, 19, tzinfo=timezone.utc)) == [("semantic", "daily")]


def test_daily_bundles_both_types_into_one_notification(monkeypatch):
    first, second = fact(), fact("今天参加了座谈", kind="episodic", note="n2")
    sent = []
    monkeypatch.setattr(fm, "queue_digest_notification", lambda space, digest: sent.append(digest))
    clock = datetime(2026, 10, 19, 19, tzinfo=timezone.utc)
    for f in [first, second]:
        fm.consolidate("s", f["memory_type"], trigger="daily", now=clock,
                       planner=lambda *args, item=f: {"actions": [create(item)]})
    assert len(sent) == 1


def test_daily_only_once_even_when_new_facts_arrive(monkeypatch):
    f = fact()
    fm.consolidate("s", "semantic", trigger="daily", planner=lambda *a: {"actions": [create(f)]})
    fact("用户住上海", note="n2")
    assert not fm.due("s")


def test_daily_digest_includes_threshold_questions_without_new_facts():
    f = fact("对象不明确", reference_state="unresolved")
    run([{"action": "defer", "source_fact_ids": [f["id"]], "reason": "缺少主体",
          "question": "这里指谁？"}])
    clock = datetime(2026, 10, 20, 19, tzinfo=timezone.utc)
    assert fm.due("s", now=clock) == [("semantic", "daily")]
    result = fm.consolidate("s", "semantic", trigger="daily", now=clock)
    assert result["digest"]["questions"]
    assert not fm.due("s", now=clock)


def test_daily_does_not_mark_done_until_tail_batch_processed(monkeypatch):
    monkeypatch.setattr(settings, "FACT_MEMORY_BATCH_SIZE", 1)
    f1, f2 = fact(), fact("用户住上海", note="n2")
    clock = datetime(2026, 10, 20, 19, tzinfo=timezone.utc)
    fm.consolidate("s", "semantic", trigger="daily", now=clock,
                   planner=lambda _s, _t, batch, *_: {"actions": [create(f) for f in batch]})
    assert fm.due("s", now=clock) == [("semantic", "daily")]
    fm.consolidate("s", "semantic", trigger="daily", now=clock,
                   planner=lambda _s, _t, batch, *_: {"actions": [create(f) for f in batch]})
    assert not fm.due("s", now=clock)


def test_batch_snapshot_does_not_consume_concurrent_arrival():
    f = fact()
    def planner(*args):
        fact("用户新买了一台电脑", note="n2")
        return {"actions": [create(f)]}
    fm.consolidate("s", "semantic", planner=planner)
    waiting = [f for f in fm.active_facts("s") if not f["processed_at"]]
    assert [f["note_id"] for f in waiting] == ["n2"]


def test_failed_run_does_not_consume_or_partially_commit():
    f = fact()
    with pytest.raises(ValueError):
        run([create(f), create(f, content="非法修改", source_fact_ids=["missing"])])
    assert not fm.cards("s")
    assert not fm.get("s", "fact", f["id"])["processed_at"]
    assert fm.items("s", "run")[0]["status"] == "failed"


def test_unaccounted_fact_fails():
    fact()
    with pytest.raises(ValueError, match="every new fact"):
        run([])


def test_inferred_card_label_time_review_and_edit_history():
    f = fact("最近连续三周负责部署")
    run([create(f, content="近期承担部署工作", assertion_kind="inferred")])
    c = fm.cards("s")[0]
    assert c["inferred_at"] and c["review_status"] == "unreviewed"
    assert any("系统归纳" in h["content"] for h in fm.query_hits("s", "部署"))
    edited = fm.review_memory("s", c["id"], decision="edit", content="仅本月临时支援部署", expected_version=c["version"])
    assert edited["assertion_kind"] == "inferred"
    assert edited["user_locked"]
    assert len(fm.items("s", "version")) == 2


def test_reject_inference_does_not_remove_source():
    f = fact()
    run([create(f, assertion_kind="inferred")])
    c = fm.cards("s")[0]
    fm.review_memory("s", c["id"], decision="reject", expected_version=c["version"])
    assert not fm.cards("s")
    assert fm.get("s", "fact", f["id"])["status"] == "active"


def test_manual_run_returns_question_without_waiting_a_day():
    f = fact("生产环境换了供应商", reference_state="unresolved")
    r = run([{"action": "defer", "source_fact_ids": [f["id"]], "reason": "两个项目都可能",
              "question": "你说的是哪个项目的生产环境？"}])
    assert r["digest"]["questions"][0]["status"] == "open"
    assert "待确认" in fm.digest_text(r["digest"])
    q = r["digest"]["questions"][0]
    fm.answer_question("s", q["id"], "随心记项目")
    assert fm.get("s", "fact", f["id"])["processed_at"] is None
    assert any(x.get("question_id") == q["id"] for x in fm.active_facts("s"))


def test_deletion_requires_preview_and_selected_ids():
    f = fact()
    run([create(f)])
    c = fm.cards("s")[0]
    request = fm.request_forget("s", "北京")
    assert fm.cards("s")
    with pytest.raises(ValueError):
        fm.approve_forget("s", request["id"], ["outside"])
    fm.approve_forget("s", request["id"], [c["id"]])
    assert not fm.cards("s")
    assert fm.get("s", "fact", f["id"])["original_text"] == "用户住北京"
    assert fact()["status"] == "forgotten"


def test_partial_delete_leaves_unselected_unrelated_memory():
    f1, f2 = fact(), fact("用户使用北京公司的电脑", note="n2")
    run([create(f1), create(f2)])
    request = fm.request_forget("s", "北京")
    fm.approve_forget("s", request["id"], [f1["id"]])
    assert fm.get("s", "fact", f2["id"])["status"] == "active"
    assert len(fm.cards("s")) == 1


def test_source_edit_invalidates_derived_memory():
    f = fact()
    run([create(f)])
    fact("用户出生在北京")
    assert not fm.cards("s")
    assert fm.get("s", "fact", f["id"])["status"] == "source_changed"


def test_cross_space_targets_rejected():
    f = fact()
    run([create(f)])
    c = fm.cards("s")[0]
    other = fact("另一个人住北京", space="other")
    with pytest.raises(ValueError, match="target"):
        run([create(other, action="revise", target_ids=[c["id"]])], space="other")
    assert fm.cards("s")[0]["version"] == 1


def test_user_edit_during_model_call_cannot_be_overwritten():
    f = fact()
    run([create(f)])
    c = fm.cards("s")[0]
    f2 = fact("用户搬到上海", note="n2")
    def planner(*args):
        fm.review_memory("s", c["id"], decision="edit", content="用户住北京，仅去上海出差", expected_version=1)
        return {"actions": [create(f2, action="revise", target_ids=[c["id"]])]}
    with pytest.raises(ValueError, match="version"):
        fm.consolidate("s", "semantic", planner=planner)
    assert "出差" in fm.cards("s")[0]["content"]


def test_merge_unions_sources_and_preserves_versions():
    f1, f2 = fact(), fact("用户目前住北京", note="n2")
    run([create(f1), create(f2)])
    old = fm.cards("s")
    f3 = fact("这两条说的是同一住址", note="n3")
    run([create(f3, action="merge", target_ids=[c["id"] for c in old],
                content="用户目前住北京", source_fact_ids=[f1["id"], f2["id"], f3["id"]])])
    assert len(fm.cards("s")) == 1
    assert len(fm.cards("s")[0]["source_fact_ids"]) == 3
    assert len(fm.items("s", "version")) == 4


def test_split_creates_two_cards():
    f1 = fact("用户在北京出生，在上海居住")
    run([create(f1)])
    c = fm.cards("s")[0]
    f2 = fact("出生地和居住地请分开记录", note="n2")
    run([{"action": "split", "target_ids": [c["id"]], "source_fact_ids": [f1["id"], f2["id"]],
          "reason": "拆开两条事实", "outputs": [create(f1, content="用户在北京出生"),
                                                   create(f1, content="用户在上海居住")]}])
    assert len(fm.cards("s")) == 2


def test_feature_gate_preserves_task_preference_pipeline(monkeypatch):
    from memory.service import process_note_memory
    process_note_memory({"id": "p", "space_id": "s", "text": "我喜欢喝咖啡"})
    process_note_memory({"id": "t", "space_id": "s", "text": "明天提醒我交报告"})
    rows = repository.list_memories("s")
    assert {r.memory_type for r in rows} >= {"task", "preference"}
    assert not fm.active_facts("s")


def test_service_stores_semantic_as_fact():
    from memory.service import process_note_memory
    result = process_note_memory({"id": "n", "space_id": "s", "text": "我住在北京"})
    assert result["results"][0]["action"] == "fact_stored"
    assert not repository.list_memories("s")
    assert len(fm.active_facts("s")) == 1


def test_link_new_fact_to_existing_card_after_revision_in_same_plan():
    f = fact()
    run([create(f)])
    c = fm.cards("s")[0]
    new = fact("用户搬到上海", note="n2")
    run([create(new, action="revise", target_ids=[c["id"]]),
         {"action": "link", "source_fact_ids": [new["id"]],
          "target_ids": [c["id"]], "reason": "迁居事件支持当前住址"}])
    assert fm.cards("s")[0]["content"] == new["content"]
    assert len(fm.items("s", "relation")) == 1


def test_preview_checks_real_contract_without_writing():
    f = fact()
    def planner(space, kind, batch, snapshot, trace):
        lease = fm.get(space, "lease", "consolidation")
        fm.validate_plan(space, {"actions": [create(f)]}, snapshot=snapshot,
                         run_id=lease["run_id"], batch_ids=[f["id"]])
        assert not fm.cards(space)
        assert fm.get(space, "fact", f["id"])["processed_at"] is None
        return {"actions": [create(f)]}
    fm.consolidate("s", "semantic", planner=planner)
    assert len(fm.items("s", "version")) == 1


def test_card_inherits_most_restrictive_source_scope():
    normal = fact()
    secret = fact("用户在北京有私人资料", note="private",
                  scope={"access_scope": "private", "sensitivity": "secret", "owner_id": "alice"})
    run([create(normal, source_fact_ids=[normal["id"], secret["id"]])])
    card = fm.cards("s")[0]
    assert card["scope"]["access_scope"] == "private"
    assert not any(r["id"] == card["id"] for r in fm.search_memory("s", "北京"))
