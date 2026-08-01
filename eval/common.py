"""文件作用：评测公共工具。

项目关系：本文件依赖 无直接本地模块依赖；被 `eval.eval_classification`、`eval.eval_memory`、`eval.eval_memory_quality`、`eval.eval_query_react` 等 9 个模块。
"""



from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """函数功能：`load_jsonl` 负责加载 jsonl，服务于本文件职责：评测公共工具。
    传参：
        path: 文件系统路径，类型为 `str | Path`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`write_json` 负责写入 json，服务于本文件职责：评测公共工具。
    传参：
        path: 文件系统路径，类型为 `str | Path`。
        data: 待处理的数据对象或结构化映射，类型为 `Any`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _get_value(data: Any, key: str, default: Any = None) -> Any:
    """函数功能：`_get_value` 负责获取 value，服务于本文件职责：评测公共工具。
    传参：
        data: 待处理的数据对象或结构化映射，类型为 `Any`。
        key: key 参数，由调用方传入，类型为 `str`。
        default: default 参数，由调用方传入，类型为 `Any`，默认值为 `None`。
    返回结果说明：
        返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def _expected_types(case: dict[str, Any]) -> list[str]:
    """函数功能：`_expected_types` 负责处理 expected types，服务于本文件职责：评测公共工具。
    传参：
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`score_classification` 负责评分 classification，服务于本文件职责：评测公共工具。
    传参：
        prediction: prediction 参数，由调用方传入，类型为 `Any`。
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
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
    """函数功能：`hit_at_k` 负责处理 hit at k，服务于本文件职责：评测公共工具。
    传参：
        ranked_ids: ranked ids 参数，由调用方传入，类型为 `list[str]`。
        expected_ids: expected ids 参数，由调用方传入，类型为 `list[str]`。
        k: k 参数，由调用方传入，类型为 `int`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if k <= 0:
        return False
    return bool(set(ranked_ids[:k]) & set(expected_ids))


def recall_at_k(ranked_ids: list[str], expected_ids: list[str], k: int) -> float:
    """函数功能：`recall_at_k` 负责处理 recall at k，服务于本文件职责：评测公共工具。
    传参：
        ranked_ids: ranked ids 参数，由调用方传入，类型为 `list[str]`。
        expected_ids: expected ids 参数，由调用方传入，类型为 `list[str]`。
        k: k 参数，由调用方传入，类型为 `int`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
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
    """函数功能：`score_retrieval` 负责评分 retrieval，服务于本文件职责：评测公共工具。
    传参：
        ranked_ids: ranked ids 参数，由调用方传入，类型为 `list[str]`。
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
        ks: ks 参数，由调用方传入，类型为 `tuple[int, ...]`，默认值为 `(1, 3, 5, 10)`。
        scores_by_id: scores by id 参数，由调用方传入，类型为 `dict[str, float] | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
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
    """函数功能：`score_query_react` 负责评分 query react，服务于本文件职责：评测公共工具。
    传参：
        tool_calls: tool calls 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        answer: answer 参数，由调用方传入，类型为 `str`。
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
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
    """函数功能：`score_summary` 负责评分 summary，服务于本文件职责：评测公共工具。
    传参：
        summary: summary 参数，由调用方传入，类型为 `str`。
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
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
    """函数功能：`aggregate_boolean_scores` 负责处理 aggregate boolean scores，服务于本文件职责：评测公共工具。
    传参：
        results: results 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        field: field 参数，由调用方传入，类型为 `str`，默认值为 `'passed'`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    total = len(results)
    passed = sum(1 for item in results if item.get(field))
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
    }
