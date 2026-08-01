"""文件作用：Note 检索评测。

项目关系：本文件依赖 `core.llm_client`、`eval.common`、`storage.vector_store`；被 `eval.eval_query_react`。
"""



from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.llm_client import embed_text
from eval.common import aggregate_boolean_scores, load_jsonl, score_retrieval, write_json
from storage.vector_store import cosine_similarity


def note_search_text(note: dict[str, Any]) -> str:
    """函数功能：`note_search_text` 负责搜索 text，服务于本文件职责：Note 检索评测。
    传参：
        note: note 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    tags = " ".join(str(tag) for tag in note.get("tags", []))
    related = " ".join(str(item) for item in note.get("related", []))
    return "\n".join(
        part
        for part in [
            str(note.get("title") or ""),
            str(note.get("type") or ""),
            tags,
            str(note.get("summary") or ""),
            str(note.get("text") or ""),
            related,
        ]
        if part
    )


def rank_notes(query: str, notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """函数功能：`rank_notes` 负责排序 notes，服务于本文件职责：Note 检索评测。
    传参：
        query: 检索或查询文本，类型为 `str`。
        notes: notes 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    query_embedding = embed_text(query)
    ranked = []
    for note in notes:
        text = note_search_text(note)
        embedding = embed_text(text)
        ranked.append(
            {
                "note_id": str(note.get("id")),
                "score": cosine_similarity(query_embedding, embedding),
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def run(cases_path: Path, *, dry_run: bool = False, max_cases: int | None = None) -> dict[str, object]:
    """函数功能：`run` 负责运行，服务于本文件职责：Note 检索评测。
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
        ranked = rank_notes(str(case.get("query", "")), list(case.get("notes", [])))
        ranked_ids = [item["note_id"] for item in ranked]
        scores_by_id = {item["note_id"]: float(item["score"]) for item in ranked}
        score = score_retrieval(ranked_ids, case, scores_by_id=scores_by_id)
        score["ranked"] = ranked
        results.append(score)

    return {
        "mode": "retrieval",
        "summary": aggregate_boolean_scores(results),
        "results": results,
    }


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：Note 检索评测。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    parser = argparse.ArgumentParser(description="Evaluate embedding retrieval quality.")
    parser.add_argument("--cases", default="eval/data/retrieval_cases.jsonl")
    parser.add_argument("--output", default="eval/results/retrieval_results.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate cases without calling embedding API")
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()

    report = run(Path(args.cases), dry_run=args.dry_run, max_cases=args.max_cases)
    write_json(args.output, report)
    print(f"Wrote {args.output}")
    print(report.get("summary", report))


if __name__ == "__main__":
    main()
