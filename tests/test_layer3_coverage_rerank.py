from memory.service import _coverage_rerank_memory_results


def _task(item_id: str, *, score: float, content: str, status: str, value: str) -> dict:
    return {
        "id": item_id,
        "score": score,
        "memory_type": "task",
        "content": content,
        "task_status": status,
        "object_value": value,
        "scope": {"canonical_topic": content.split(":")[0]},
    }


def test_task_inventory_rerank_prefers_explicit_state_records_without_logical_refs() -> None:
    results = [
        _task("free_text_todo", score=0.48, content="论文发言稿待处理", status="todo", value="论文发言稿"),
        _task("done", score=0.4723, content="上下文工程实验:done", status="done", value="done"),
        _task("free_text_other", score=0.4648, content="思维导图连线修复待处理", status="todo", value="思维导图连线修复"),
        _task("todo", score=0.4505, content="随心记评测:todo", status="todo", value="todo"),
        _task("blocked", score=0.4436, content="检索质量优化:blocked", status="blocked", value="blocked"),
    ]

    ranked = _coverage_rerank_memory_results(results, query="列出我当前三个项目的状态", limit=3)

    assert [item["id"] for item in ranked] == ["done", "todo", "blocked"]


def test_non_inventory_query_preserves_repository_order() -> None:
    results = [_task("a", score=0.8, content="甲:todo", status="todo", value="todo"), _task("b", score=0.7, content="乙:todo", status="todo", value="todo")]

    assert _coverage_rerank_memory_results(results, query="甲现在是什么状态？", limit=2) == results
