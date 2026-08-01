"""文件作用：总结评测。

项目关系：本文件依赖 `core.llm_client`、`eval.common`、`summary.daily_summary`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.llm_client import complete_json
from eval.common import aggregate_boolean_scores, load_jsonl, score_summary, write_json
from summary.daily_summary import REFLECTION_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT


def _stats(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """函数功能：`_stats` 负责处理 stats，服务于本文件职责：总结评测。
    传参：
        notes: notes 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    type_counter = Counter(str(note.get("type") or "未分类") for note in notes)
    tag_counter: Counter[str] = Counter()
    for note in notes:
        tag_counter.update(str(tag) for tag in note.get("tags", []))
    return {
        "note_count": len(notes),
        "type_counts": dict(type_counter.most_common()),
        "top_tags": dict(tag_counter.most_common(20)),
    }


def generate_case_summary(case: dict[str, Any]) -> str:
    """函数功能：`generate_case_summary` 负责生成 case summary，服务于本文件职责：总结评测。
    传参：
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    notes = list(case.get("notes", []))
    payload = {
        "range_label": case.get("range_label", "评测范围"),
        "start": case.get("start"),
        "end": case.get("end"),
        "stats": _stats(notes),
        "notes": notes,
    }
    draft = complete_json(
        system_prompt=SUMMARY_SYSTEM_PROMPT,
        user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
    ).get("summary_markdown", "")
    reviewed = complete_json(
        system_prompt=REFLECTION_SYSTEM_PROMPT,
        user_prompt=json.dumps({"notes": notes, "draft": draft}, ensure_ascii=False, indent=2),
    ).get("final_summary", "")
    return str(reviewed or draft).strip()


def run(cases_path: Path, *, dry_run: bool = False, max_cases: int | None = None) -> dict[str, object]:
    """函数功能：`run` 负责运行，服务于本文件职责：总结评测。
    传参：
        cases_path: cases path 参数，由调用方传入，类型为 `Path`。
        dry_run: dry run 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        max_cases: max cases 参数，由调用方传入，类型为 `int | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, object]`，表示结构化结果、载荷或状态映射。
    """
    cases = load_jsonl(cases_path)
    if max_cases is not None:
        cases = cases[:max_cases]

    if dry_run:
        return {"mode": "dry_run", "cases": len(cases), "case_ids": [case.get("case_id") for case in cases]}

    results = []
    for case in cases:
        summary = generate_case_summary(case)
        score = score_summary(summary, case)
        score["summary"] = summary
        results.append(score)

    return {
        "mode": "summary",
        "summary": aggregate_boolean_scores(results),
        "results": results,
    }


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：总结评测。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    parser = argparse.ArgumentParser(description="Evaluate summary coverage quality.")
    parser.add_argument("--cases", default="eval/data/summary_cases.jsonl")
    parser.add_argument("--output", default="eval/results/summary_results.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate cases without calling LLM")
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()

    report = run(Path(args.cases), dry_run=args.dry_run, max_cases=args.max_cases)
    write_json(args.output, report)
    print(f"Wrote {args.output}")
    print(report.get("summary", report))


if __name__ == "__main__":
    main()
