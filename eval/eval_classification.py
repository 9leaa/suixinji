"""文件作用：Note 分类评测。

项目关系：本文件依赖 `core.classifier`、`eval.common`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.classifier import classify_text
from eval.common import aggregate_boolean_scores, load_jsonl, score_classification, write_json


def run(cases_path: Path, *, dry_run: bool = False, max_cases: int | None = None) -> dict[str, object]:
    """函数功能：`run` 负责运行，服务于本文件职责：Note 分类评测。
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
        prediction = classify_text(str(case.get("text", "")))
        results.append(score_classification(prediction, case))

    return {
        "mode": "classification",
        "summary": aggregate_boolean_scores(results),
        "results": results,
    }


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：Note 分类评测。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    parser = argparse.ArgumentParser(description="Evaluate note classification quality.")
    parser.add_argument("--cases", default="eval/data/classification_cases.jsonl")
    parser.add_argument("--output", default="eval/results/classification_results.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate cases without calling LLM")
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()

    report = run(Path(args.cases), dry_run=args.dry_run, max_cases=args.max_cases)
    write_json(args.output, report)
    print(f"Wrote {args.output}")
    print(report.get("summary", report))


if __name__ == "__main__":
    main()
