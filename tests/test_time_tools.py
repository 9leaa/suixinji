from unittest.mock import Mock
import pytest
from core.time_tools import resolve_range, resolve_due
from core import settings
from memory import extractor, service, repository as repo, task_cards, task_retrieval
from memory.task_schema import TaskInput
from memory.time_query import parse_time_query, answer_time_query


@pytest.mark.parametrize(
    "expression,reference,start,end",
    [
        ("本周", "2026-10-20T09:00:00+08:00", "2026-10-19", "2026-10-25"),
        ("下周", "2026-10-25T23:59:00+08:00", "2026-10-26", "2026-11-01"),
        ("上周", "2026-10-26T00:00:00+08:00", "2026-10-19", "2026-10-25"),
        ("本周", "2026-01-01T09:00:00+08:00", "2025-12-29", "2026-01-04"),
        ("下个月", "2028-01-31T09:00:00+08:00", "2028-02-01", "2028-02-29"),
        ("最近三天", "2026-10-20T09:00:00+08:00", "2026-10-18", "2026-10-20"),
        ("本周", "2026-10-25T17:00:00+00:00", "2026-10-26", "2026-11-01"),
        ("2026-10-19到2026-10-25", "2026-11-01T09:00:00+08:00", "2026-10-19", "2026-10-25"),
    ],
)
def test_calendar(expression, reference, start, end):
    result = resolve_range(expression, reference)
    assert (result["start"], result["end"]) == (start, end)


def test_precision_vague_and_invalid():
    assert resolve_due("周五前", "2026-10-20T09:00:00+08:00") == "2026-10-23"
    assert resolve_due("明天下午3点", "2026-10-20T09:00:00+08:00") == "2026-10-21T15:00:00+08:00"
    assert resolve_due("过阵子", "2026-10-20T09:00:00+08:00") is None
    with pytest.raises(ValueError):
        resolve_range("本周", None)
    with pytest.raises(ValueError):
        resolve_range("2026-10-25到2026-10-19", "2026-10-20")


@pytest.fixture
def writer(monkeypatch):
    monkeypatch.setattr(settings, "TASK_CARDS_ENABLED", True)
    for mod in (extractor, service):
        monkeypatch.setattr(mod, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(task_retrieval, "vector_rank", lambda *a: [])

    def write(text, ts, **kw):
        data = TaskInput(task="提交周报", task_status="todo", evidence_span=text, description=text, **kw)
        monkeypatch.setattr(extractor, "complete_json", Mock(return_value={"candidates": [data.model_dump()]}))
        note = {"id": ts, "space_id": "s", "text": text, "ts": ts}
        result = service.process_note_memory(note)
        return result

    return write


def test_delay_preserves_period_and_query_fields(writer, monkeypatch):
    first = writer("本周提交周报，周五前交", "2026-10-20T09:00:00+08:00", period_expression="本周", due_expression="周五")
    card = first["results"][0]["task_card"]
    assert card["period_start"] == "2026-10-19" and card["due_at"] == "2026-10-23"
    monkeypatch.setattr(
        task_cards,
        "complete_json",
        Mock(
            return_value={
                "action": "update",
                "target_id": card["id"],
                "description": "本周周报延期到下周一",
                "subject_relation": "same",
                "instance_relation": "same",
                "reason": "延期不改变周期",
            }
        ),
    )
    second = writer("周报延期到下周一交", "2026-10-22T09:00:00+08:00", due_expression="下周一", intent="update")
    after = second["results"][0]["task_card"]
    assert after["id"] == card["id"]
    assert after["period_start"] == "2026-10-19" and after["due_at"] == "2026-10-26"
    assert after["sources"][-1]["resolved_time"]["time_expression"]["due"] == "下周一"
    assert repo.get_memory(card["id"]).current_version == 2
    query = parse_time_query("列出下周到期的未完成任务")
    assert query["kind"] == "due"
    assert "共 1 条" in answer_time_query("s", query, now="2026-10-20T09:00:00+08:00")
    assert "共 0 条" in answer_time_query("s", query, owner="other", now="2026-10-20T09:00:00+08:00")
    assert parse_time_query("列出下周给客户的任务") is None


def test_fabricated_time_rejected(writer):
    result = writer("提交周报", "2026-10-20T09:00:00+08:00", period_expression="本周")
    assert not list(task_cards.records("s", "owner"))
    assert result["extraction_status"] in {"completed", "pending_review", "failed"}


def test_message_range_and_boundary(monkeypatch):
    from storage import note_storage

    monkeypatch.setattr(
        note_storage,
        "load_queryable_index",
        lambda _: [
            {"text": "第一条", "ts": "2026-10-25T15:59:59+00:00"},
            {"text": "下周消息", "ts": "2026-10-25T16:00:00+00:00"},
            {"text": "别人的消息", "ts": "2026-10-20T09:00:00+08:00", "user_id": "other"},
        ],
    )
    query = parse_time_query("2026-10-19到2026-10-25我记录了什么？")
    answer = answer_time_query("s", query, now="2026-10-26T09:00:00+08:00")
    assert "共 1 条" in answer and "第一条" in answer and "下周消息" not in answer


def test_query_not_top_k_limited(writer):
    writer("本周提交周报", "2026-10-20T09:00:00+08:00", period_expression="本周")
    # The query iterates the paginated repository reader, not a semantic top-k.
    query = parse_time_query("列出本周的任务")
    assert "共 1 条" in answer_time_query("s", query, now="2026-10-20T09:00:00+08:00")


def test_chinese_interval_and_month_deadline():
    from core.time_tools import task_time_fields

    assert resolve_range("10月19日到10月25日", "2026-10-26T09:00:00+08:00")["end"] == "2026-10-25"
    assert parse_time_query("帮我查一下从10月19日到10月25日的内容")["kind"] == "recorded"
    value = TaskInput(
        task="设备巡检", task_status="todo", evidence_span="下个月设备巡检，月底前完成", period_expression="下个月", due_expression="月底前"
    )
    fields = task_time_fields(value, {"text": value.evidence_span, "ts": "2028-01-31T09:00:00+08:00"})
    assert fields["due_at"] == "2028-02-29"


def test_time_list_binds_completion_numbers(writer, monkeypatch):
    from memory import task_commands
    from agent.query_agent import answer_question

    monkeypatch.setattr(task_commands, "wait_for_memory_barrier", lambda *a: {"status": "ready"})
    writer("本周提交周报", "2026-10-20T09:00:00+08:00", period_expression="本周")
    answer = answer_question("s", "列出2026-10-19到2026-10-25的任务")
    assert "任务 1：" in answer
    assert "已标记完成" in answer_question("s", "任务1请标记完成")


def test_full_time_query_over_100(writer, monkeypatch):
    writer("本周提交周报", "2026-10-20T09:00:00+08:00", period_expression="本周")
    record = next(task_cards.records("s", "owner"))
    monkeypatch.setattr(task_cards, "records", lambda *a: iter([record] * 125))
    answer = answer_time_query("s", parse_time_query("列出本周任务"), now="2026-10-20T09:00:00+08:00")
    assert "共 125 条" in answer and "任务 125：" in answer
