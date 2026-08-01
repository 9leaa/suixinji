"""文件作用：相关召回、合并和冲突处理。

项目关系：本文件依赖 `memory.consolidator`、`memory.models`、`memory.repository`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from memory.consolidator import generate_stable_semantic, merge_duplicate_episodic, process_unextracted_notes
from memory.models import MemoryCandidate
from memory.repository import insert_memory, list_memories, list_memory_relations


def test_process_unextracted_notes_processes_notes_without_memory_sources(monkeypatch):
    """函数功能：`test_process_unextracted_notes_processes_notes_without_memory_sources` 负责验证 process unextracted notes processes notes without memory sources 场景，服务于本文件职责：相关召回、合并和冲突处理。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    notes = [
        {"id": "note-1", "space_id": "space-1", "text": "我正在学习 Agent 工程"},
        {"id": "note-2", "space_id": "space-1", "text": "你好"},
    ]
    monkeypatch.setattr("memory.consolidator.load_index", lambda space_id: notes)

    report = process_unextracted_notes("space-1")

    assert report["processed_count"] == 2
    assert list_memories("space-1", status="active")


def test_merge_duplicate_episodic_preserves_sources_and_supersedes_duplicate():
    """函数功能：`test_merge_duplicate_episodic_preserves_sources_and_supersedes_duplicate` 负责验证 merge duplicate episodic preserves sources and supersedes duplicate 场景，服务于本文件职责：相关召回、合并和冲突处理。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    first = insert_memory("space-1", MemoryCandidate("episodic", "今天阅读 RAG 论文", 0.6, 0.8), source_note_id="note-1")
    second = insert_memory("space-1", MemoryCandidate("episodic", "今天阅读 RAG 论文", 0.6, 0.8), source_note_id="note-2")

    report = merge_duplicate_episodic("space-1", min_score=0.1)

    active = list_memories("space-1", status="active", memory_type="episodic")
    superseded = list_memories("space-1", status="superseded", memory_type="episodic")
    assert report["merged_count"] == 1
    assert len(active) == 1
    assert len(active[0].sources) == 2
    assert len(superseded) == 1
    assert {active[0].id, superseded[0].id} == {first.id, second.id}


def test_generate_stable_semantic_keeps_source_notes_and_original_episodic_memories():
    """函数功能：`test_generate_stable_semantic_keeps_source_notes_and_original_episodic_memories` 负责验证 generate stable semantic keeps source notes and original episodic memories 场景，服务于本文件职责：相关召回、合并和冲突处理。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    for idx, text in enumerate(["阅读 RAG 论文", "实现向量检索", "调整 ReAct 查询"]):
        insert_memory("space-1", MemoryCandidate("episodic", text, 0.6, 0.8), source_note_id=f"note-{idx}")

    report = generate_stable_semantic("space-1", min_sources=3)

    assert report["created"] is True
    semantic = list_memories("space-1", status="active", memory_type="semantic")
    episodic = list_memories("space-1", status="active", memory_type="episodic")
    assert semantic
    assert len(semantic[0].sources) == 3
    assert len([relation for relation in list_memory_relations(semantic[0].id) if relation.relation == "summarized_from"]) == 3
    assert len(episodic) == 3
