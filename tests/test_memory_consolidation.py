from memory.consolidator import generate_stable_semantic, merge_duplicate_episodic, process_unextracted_notes
from memory.models import MemoryCandidate
from memory.repository import insert_memory, list_memories


def test_process_unextracted_notes_processes_notes_without_memory_sources(monkeypatch):
    notes = [
        {"id": "note-1", "space_id": "space-1", "text": "我正在学习 Agent 工程"},
        {"id": "note-2", "space_id": "space-1", "text": "你好"},
    ]
    monkeypatch.setattr("memory.consolidator.load_index", lambda space_id: notes)

    report = process_unextracted_notes("space-1")

    assert report["processed_count"] == 2
    assert list_memories("space-1", status="active")


def test_merge_duplicate_episodic_preserves_sources_and_supersedes_duplicate():
    first = insert_memory("space-1", MemoryCandidate("episodic", "今天阅读 RAG 论文", 0.6, 0.8), source_note_id="note-1")
    second = insert_memory("space-1", MemoryCandidate("episodic", "今天阅读 RAG 论文", 0.6, 0.8), source_note_id="note-2")

    report = merge_duplicate_episodic("space-1", min_score=0.1)

    keeper = next(memory for memory in list_memories("space-1", status="active", memory_type="episodic") if memory.id == first.id)
    superseded = list_memories("space-1", status="superseded", memory_type="episodic")
    assert report["merged_count"] == 1
    assert len(keeper.sources) == 2
    assert superseded[0].id == second.id


def test_generate_stable_semantic_keeps_source_notes_and_original_episodic_memories():
    for idx, text in enumerate(["阅读 RAG 论文", "实现向量检索", "调整 ReAct 查询"]):
        insert_memory("space-1", MemoryCandidate("episodic", text, 0.6, 0.8), source_note_id=f"note-{idx}")

    report = generate_stable_semantic("space-1", min_sources=3)

    assert report["created"] is True
    semantic = list_memories("space-1", status="active", memory_type="semantic")
    episodic = list_memories("space-1", status="active", memory_type="episodic")
    assert semantic
    assert len(semantic[0].sources) == 3
    assert len(episodic) == 3
