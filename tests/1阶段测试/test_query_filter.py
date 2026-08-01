"""文件作用：Note 标签/类型过滤。

项目关系：本文件依赖 `agent`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from agent import query_agent

SPACE_ID = "space_test"


NOTES = [
    {
        "id": "n1",
        "message_id": "m1",
        "space_id": SPACE_ID,
        "ts": "2026-06-07T09:00:00+08:00",
        "title": "早餐",
        "type": "生活",
        "tags": ["饮食", "日常"],
        "summary": "吃了早餐。",
        "text": "早上吃了馅饼。",
        "related": [],
    },
    {
        "id": "n2",
        "message_id": "m2",
        "space_id": SPACE_ID,
        "ts": "2026-06-07T11:00:00+08:00",
        "title": "游泳计划",
        "type": "生活",
        "tags": ["运动", "计划"],
        "summary": "计划去游泳。",
        "text": "今天想去游泳。",
        "related": ["n1"],
    },
    {
        "id": "n3",
        "message_id": "m3",
        "space_id": SPACE_ID,
        "ts": "2026-06-07T12:00:00+08:00",
        "title": "P4 测试",
        "type": "任务",
        "tags": ["待办", "提醒"],
        "summary": "测试 P4 总结。",
        "text": "记得测试 P4 总结功能。",
        "related": ["n2"],
    },
]


def patch_notes(monkeypatch):
    """函数功能：`patch_notes` 负责处理 patch notes，服务于本文件职责：Note 标签/类型过滤。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(query_agent, "load_index", lambda space_id: list(NOTES))


def ids(items):
    """函数功能：`ids` 负责处理 ids，服务于本文件职责：Note 标签/类型过滤。
    传参：
        items: 待遍历或处理的元素集合。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    return [item["id"] for item in items]


def test_filter_notes_by_type_sorts_desc(monkeypatch):
    """函数功能：`test_filter_notes_by_type_sorts_desc` 负责验证 filter notes by type sorts desc 场景，服务于本文件职责：Note 标签/类型过滤。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    patch_notes(monkeypatch)

    results = query_agent.filter_notes(SPACE_ID, note_type="生活")

    assert ids(results) == ["n2", "n1"]
    assert all(item["type"] == "生活" for item in results)


def test_filter_notes_by_all_tags(monkeypatch):
    """函数功能：`test_filter_notes_by_all_tags` 负责验证 filter notes by all tags 场景，服务于本文件职责：Note 标签/类型过滤。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    patch_notes(monkeypatch)

    results = query_agent.filter_notes(
        SPACE_ID,
        note_type="生活",
        tags=["饮食", "日常"],
        match_all_tags=True,
    )

    assert ids(results) == ["n1"]


def test_filter_notes_by_any_tag(monkeypatch):
    """函数功能：`test_filter_notes_by_any_tag` 负责验证 filter notes by any tag 场景，服务于本文件职责：Note 标签/类型过滤。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    patch_notes(monkeypatch)

    results = query_agent.filter_notes(
        SPACE_ID,
        tags=["饮食", "运动"],
        match_all_tags=False,
    )

    assert ids(results) == ["n2", "n1"]


def test_filter_notes_rejects_invalid_type_or_tag(monkeypatch):
    """函数功能：`test_filter_notes_rejects_invalid_type_or_tag` 负责验证 filter notes rejects invalid type or tag 场景，服务于本文件职责：Note 标签/类型过滤。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    patch_notes(monkeypatch)

    assert query_agent.filter_notes(SPACE_ID, note_type="工作") == []
    assert query_agent.filter_notes(SPACE_ID, tags=["自由标签"]) == []


def test_by_type_and_by_tag_are_thin_wrappers(monkeypatch):
    """函数功能：`test_by_type_and_by_tag_are_thin_wrappers` 负责验证 by type and by tag are thin wrappers 场景，服务于本文件职责：Note 标签/类型过滤。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    patch_notes(monkeypatch)

    assert ids(query_agent.by_type(SPACE_ID, "任务")) == ["n3"]
    assert ids(query_agent.by_tag(SPACE_ID, "提醒")) == ["n3"]


def test_get_note_and_follow_links(monkeypatch):
    """函数功能：`test_get_note_and_follow_links` 负责验证 get note and follow links 场景，服务于本文件职责：Note 标签/类型过滤。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    patch_notes(monkeypatch)

    note = query_agent.get_note(SPACE_ID, "n3")
    assert note["title"] == "P4 测试"

    linked = query_agent.follow_links(SPACE_ID, "n2")
    assert linked["source"]["id"] == "n2"
    assert ids(linked["outbound_related"]) == ["n1"]
    assert ids(linked["inbound_related"]) == ["n3"]


def test_run_tool_coerces_filter_args(monkeypatch):
    """函数功能：`test_run_tool_coerces_filter_args` 负责验证 run tool coerces filter args 场景，服务于本文件职责：Note 标签/类型过滤。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    patch_notes(monkeypatch)

    result = query_agent._run_tool(
        SPACE_ID,
        "filter_notes",
        {"type": "生活", "tags": "饮食", "match_all_tags": "true"},
    )

    assert ids(result) == ["n1"]
