"""Small end-to-end smoke dataset for LLM/rule preference scope handling."""

from __future__ import annotations

import json
from pathlib import Path

from memory import extractor


DATASET = Path(__file__).parents[1] / "eval" / "datasets" / "suixinji_scope_smoke_v1.jsonl"


def _llm_row(text: str) -> dict[str, object]:
    if text == "早上喜欢咖啡":
        scope = "早上"
        polarity = "positive"
    elif text == "工作时不喝咖啡":
        scope = None
        polarity = "negative"
    elif text == "我喜欢咖啡":
        scope = None
        polarity = "unknown"
    else:
        # Deliberately return only one same-clause fact so Hybrid must add the
        # second scoped preference from the rule extractor.
        scope = "早上"
        polarity = "positive"
        text = "早上喜欢咖啡"
    return {
        "memory_type": "preference",
        "entity": "用户",
        "attribute": "preference",
        "canonical_topic": "咖啡",
        "new_value": "咖啡",
        "content": text,
        "evidence_span": text,
        "polarity": polarity,
        "scope": scope,
        "confidence": 0.9,
        "importance": 0.8,
        "should_store": True,
        "extraction_reason": "scope smoke",
    }


def test_scope_smoke_dataset_runs_end_to_end(monkeypatch):
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")
    monkeypatch.setattr(extractor.settings, "MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED", True)

    def fake_complete_json(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        text = str(payload["text"])
        if text == "早上喜欢咖啡，晚上不喜欢咖啡":
            return {"candidates": [_llm_row(text)]}
        return {"candidates": [_llm_row(text)]}

    monkeypatch.setattr(extractor, "complete_json", fake_complete_json)

    for index, row in enumerate(rows):
        candidates = extractor.extract_candidates(f"scope-smoke-{index}", row["text"])
        preferences = [candidate for candidate in candidates if candidate.memory_type == "preference"]
        expected = row["expected"]
        if "scopes" in expected:
            assert sorted(candidate.scope["scope"] for candidate in preferences) == sorted(expected["scopes"])
            assert sorted(candidate.polarity for candidate in preferences) == sorted(expected["polarities"])
            continue
        assert len(preferences) == 1
        candidate = preferences[0]
        assert candidate.object_value == expected["topic"]
        assert candidate.scope["scope"] == expected["scope"]
        assert candidate.scope.get("scope_source") == expected.get("scope_source", candidate.scope.get("scope_source"))
        assert candidate.scope.get("scope_explicit", False) == expected.get("scope_explicit", True)
        assert candidate.polarity == expected["polarity"]
