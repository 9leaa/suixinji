"""Shared helpers for offline evaluation scripts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"Expected object at {path}:{line_no}")
            items.append(item)
    return items


def write_json(path: str | Path, data: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _get_value(data: Any, key: str, default: Any = None) -> Any:
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def _expected_types(case: dict[str, Any]) -> list[str]:
    values = case.get("acceptable_types")
    if values is None:
        values = case.get("expected_types")
    if values is None:
        expected_type = case.get("expected_type")
        return [str(expected_type)] if expected_type is not None else []
    if isinstance(values, str):
        return [values]
    return [str(item) for item in values]


def score_classification(prediction: Any, case: dict[str, Any]) -> dict[str, Any]:
    pred_type = str(_get_value(prediction, "type", ""))
    pred_tags = set(str(tag) for tag in (_get_value(prediction, "tags", []) or []))

    acceptable_types = _expected_types(case)
    expected_tags_any = set(str(tag) for tag in case.get("expected_tags_any", []))
    expected_tags_all = set(str(tag) for tag in case.get("expected_tags_all", []))
    min_tag_hits = int(case.get("min_tag_hits", 2 if expected_tags_any else 0))

    matched_any_tags = sorted(pred_tags & expected_tags_any)
    type_ok = not acceptable_types or pred_type in acceptable_types
    tags_any_hits = len(matched_any_tags)
    tags_any_ok = not expected_tags_any or tags_any_hits >= min_tag_hits
    tags_all_ok = expected_tags_all.issubset(pred_tags)
    passed = type_ok and tags_any_ok and tags_all_ok

    return {
        "case_id": case.get("case_id"),
        "passed": passed,
        "type_ok": type_ok,
        "tags_any_ok": tags_any_ok,
        "tags_all_ok": tags_all_ok,
        "tags_any_hits": tags_any_hits,
        "min_tag_hits": min_tag_hits,
        "matched_any_tags": matched_any_tags,
        "pred_type": pred_type,
        "pred_tags": sorted(pred_tags),
        "expected_type": case.get("expected_type"),
        "acceptable_types": acceptable_types,
        "expected_tags_any": sorted(expected_tags_any),
        "expected_tags_all": sorted(expected_tags_all),
    }


def hit_at_k(ranked_ids: list[str], expected_ids: list[str], k: int) -> bool:
    if k <= 0:
        return False
    return bool(set(ranked_ids[:k]) & set(expected_ids))


def recall_at_k(ranked_ids: list[str], expected_ids: list[str], k: int) -> float:
    expected = set(expected_ids)
    if not expected or k <= 0:
        return 0.0
    found = set(ranked_ids[:k]) & expected
    return len(found) / len(expected)


def score_retrieval(
    ranked_ids: list[str],
    case: dict[str, Any],
    ks: tuple[int, ...] = (1, 3, 5, 10),
    scores_by_id: dict[str, float] | None = None,
) -> dict[str, Any]:
    expected_ids = [str(item) for item in case.get("expected_note_ids", [])]
    scores_by_id = scores_by_id or {}
    result: dict[str, Any] = {
        "case_id": case.get("case_id"),
        "ranked_ids": ranked_ids,
        "expected_note_ids": expected_ids,
        "expected_no_result": bool(case.get("expected_no_result", False)),
    }

    for k in ks:
        result[f"hit@{k}"] = hit_at_k(ranked_ids, expected_ids, k)
        result[f"recall@{k}"] = round(recall_at_k(ranked_ids, expected_ids, k), 4)

    if result["expected_no_result"]:
        min_score = float(case.get("min_score", 0.55))
        max_score = max(scores_by_id.values(), default=0.0)
        result["min_score"] = min_score
        result["max_score"] = round(max_score, 4)
        result["no_result_ok"] = max_score < min_score
        result["passed"] = result["no_result_ok"]
        return result

    pass_k = int(case.get("pass_k", 5 if len(expected_ids) > 1 else 3))
    min_recall = float(case.get("min_recall", 1.0))
    result["pass_k"] = pass_k
    result["min_recall"] = min_recall
    result["passed"] = result.get(f"recall@{pass_k}", 0.0) >= min_recall
    return result


def score_query_react(
    tool_calls: list[dict[str, Any]],
    answer: str,
    case: dict[str, Any],
) -> dict[str, Any]:
    tools_used = [str(call.get("tool")) for call in tool_calls]
    expected_tools_all = [str(item) for item in case.get("expected_tools_all", [])]
    expected_tools_any = [str(item) for item in case.get("expected_tools_any", [])]
    expected_note_ids = [str(item) for item in case.get("expected_note_ids", [])]
    answer_must_include = [str(item) for item in case.get("answer_must_include", [])]

    observed_note_ids: list[str] = []
    for call in tool_calls:
        result = call.get("result")
        if isinstance(result, list):
            observed_note_ids.extend(str(item.get("id")) for item in result if isinstance(item, dict) and item.get("id"))
        elif isinstance(result, dict):
            if result.get("id"):
                observed_note_ids.append(str(result.get("id")))
            for key in ("related", "candidates"):
                for item in result.get(key, []) if isinstance(result.get(key), list) else []:
                    if isinstance(item, dict) and item.get("id"):
                        observed_note_ids.append(str(item.get("id")))

    tools_all_ok = all(tool in tools_used for tool in expected_tools_all)
    tools_any_ok = not expected_tools_any or any(tool in tools_used for tool in expected_tools_any)
    notes_ok = not expected_note_ids or bool(set(expected_note_ids) & set(observed_note_ids))
    answer_ok = all(term in answer for term in answer_must_include)

    return {
        "case_id": case.get("case_id"),
        "passed": tools_all_ok and tools_any_ok and notes_ok and answer_ok,
        "tools_used": tools_used,
        "expected_tools_all": expected_tools_all,
        "expected_tools_any": expected_tools_any,
        "tools_all_ok": tools_all_ok,
        "tools_any_ok": tools_any_ok,
        "observed_note_ids": observed_note_ids,
        "expected_note_ids": expected_note_ids,
        "notes_ok": notes_ok,
        "answer_ok": answer_ok,
        "answer": answer,
    }


def score_summary(summary: str, case: dict[str, Any]) -> dict[str, Any]:
    must_include = [str(item) for item in case.get("must_include", [])]
    must_not_include = [str(item) for item in case.get("must_not_include", [])]

    missing = [item for item in must_include if item not in summary]
    forbidden = [item for item in must_not_include if item in summary]
    passed = not missing and not forbidden

    return {
        "case_id": case.get("case_id"),
        "passed": passed,
        "missing": missing,
        "forbidden": forbidden,
        "must_include_count": len(must_include),
        "covered_count": len(must_include) - len(missing),
        "summary_length": len(summary),
    }


def aggregate_boolean_scores(results: list[dict[str, Any]], field: str = "passed") -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for item in results if item.get(field))
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
    }
