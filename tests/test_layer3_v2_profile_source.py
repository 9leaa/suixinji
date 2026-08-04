from __future__ import annotations

from agent.query_agent import _build_evidence_bundle


def test_profile_tool_bundle_retains_per_slot_sources():
    result = {
        "slots": {
            "preference": [{"id": "m1", "memory_type": "preference", "content": "喜欢绿茶", "sources": [{"note_id": "s1"}]}],
            "semantic": [
                {"id": "m2", "memory_type": "semantic", "content": "主要使用iPad", "sources": [{"note_id": "s2"}]},
                {"id": "m3", "memory_type": "semantic", "content": "常用英文", "sources": [{"note_id": "s3"}]},
            ],
        }
    }
    bundle = _build_evidence_bundle(result, [{"tool": "profile_summary", "result": result}])

    assert bundle.selected_memory_ids == ["m1", "m2", "m3"]
    assert bundle.selected_source_ids == ["s1", "s2", "s3"]
    assert bundle.selected_tool_refs == ["profile_summary"]
