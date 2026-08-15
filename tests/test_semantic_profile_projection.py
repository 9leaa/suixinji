from __future__ import annotations

import json

from core import llm_client
from memory import semantic_profile_projection as projection
from memory.models import MemoryRecord


def _semantic(memory_id: str, content: str) -> MemoryRecord:
    return MemoryRecord(
        id=memory_id,
        space_id="profile-space",
        memory_type="semantic",
        content=content,
        normalized_content=content,
        importance=0.8,
        confidence=0.9,
        status="active",
        valid_from=None,
        valid_until=None,
        created_at="2026-08-15T00:00:00+00:00",
        updated_at="2026-08-15T00:00:00+00:00",
        last_accessed_at=None,
        access_count=0,
        current_version=1,
        subject="用户",
        predicate="location",
        object_value=content,
    )


def test_stale_projection_overlays_unprojected_semantic_fact(monkeypatch) -> None:
    old = _semantic("mem-old", "用户住在北京")
    latest = _semantic("mem-new", "用户搬到上海")
    monkeypatch.setattr(
        projection,
        "get_semantic_profile_projections",
        lambda _space: {
            "location": {
                "projection": {
                    "summary": "用户此前住在北京",
                    "current_memory_ids": ["mem-old"],
                    "uncertain_memory_ids": [],
                },
                "source_memory_ids": ["mem-old"],
                "processed_revision": 3,
                "target_revision": 4,
            }
        },
    )
    captured: dict[str, object] = {}

    def fake_complete_json(*, user_prompt: str, **_kwargs):
        captured["payload"] = json.loads(user_prompt)
        return {"profile_lines": ["用户目前住在上海"], "uncertain_lines": []}

    monkeypatch.setattr(llm_client, "complete_json", fake_complete_json)
    lines, uncertain = projection.semantic_profile_lines("profile-space", [old, latest]) or ([], [])
    assert lines == ["用户目前住在上海"]
    assert uncertain == []
    facets = captured["payload"]["facets"]
    assert facets[0]["delta_facts"][0]["id"] == "mem-new"


def test_refresh_projection_persists_auditable_source_ids(monkeypatch) -> None:
    memory = _semantic("mem-shanghai", "用户搬到上海")
    monkeypatch.setattr(
        projection,
        "get_semantic_profile_projection",
        lambda _space, _facet: {"processed_revision": 2, "target_revision": 3},
    )
    saved: dict[str, object] = {}
    monkeypatch.setattr(
        projection,
        "save_semantic_profile_projection",
        lambda _space, _facet, **kwargs: saved.update(kwargs) or True,
    )
    monkeypatch.setattr(
        llm_client,
        "complete_json",
        lambda **_kwargs: {
            "summary": "用户目前住在上海",
            "current_memory_ids": ["mem-shanghai"],
            "uncertain_memory_ids": [],
        },
    )

    assert projection.refresh_semantic_profile_projection("profile-space", "location", [memory]) is True
    assert saved["expected_revision"] == 3
    assert saved["source_memory_ids"] == ["mem-shanghai"]
    assert saved["projection"]["current_memory_ids"] == ["mem-shanghai"]

