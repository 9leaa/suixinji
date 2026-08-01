"""文件作用：Agent 问答评测。

项目关系：本文件依赖 `agent`、`core.llm_client`、`eval.common`、`eval.eval_retrieval` 等 5 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import query_agent
from core.llm_client import embed_text
from eval.common import aggregate_boolean_scores, load_jsonl, score_query_react, write_json
from eval.eval_retrieval import note_search_text
from storage.vector_store import cosine_similarity


def _clip(text: str | None, limit: int = 500) -> str:
    """函数功能：`_clip` 负责处理 clip，服务于本文件职责：Agent 问答评测。
    传参：
        text: 输入文本内容，类型为 `str | None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `500`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def local_semantic_search(notes: list[dict[str, Any]], query: str, top_k: int, min_score: float) -> list[dict[str, Any]]:
    """函数功能：`local_semantic_search` 负责搜索 local semantic，服务于本文件职责：Agent 问答评测。
    传参：
        notes: notes 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        query: 检索或查询文本，类型为 `str`。
        top_k: top k 参数，由调用方传入，类型为 `int`。
        min_score: min score 参数，由调用方传入，类型为 `float`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    query_embedding = embed_text(query)
    ranked = []
    for note in notes:
        embedding = embed_text(note_search_text(note))
        score = cosine_similarity(query_embedding, embedding)
        if score < min_score:
            continue
        ranked.append((score, note))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "id": note.get("id"),
            "message_id": note.get("message_id"),
            "score": round(score, 4),
            "title": note.get("title"),
            "type": note.get("type"),
            "tags": note.get("tags", []),
            "summary": note.get("summary"),
            "time": note.get("ts") or note.get("time"),
            "text": _clip(note.get("text")),
        }
        for score, note in ranked[:top_k]
    ]


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """函数功能：`run_case` 负责运行 case，服务于本文件职责：Agent 问答评测。
    传参：
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    notes = list(case.get("notes", []))
    tool_calls: list[dict[str, Any]] = []

    original_load_index = query_agent.load_index
    original_semantic_search = query_agent.semantic_search
    original_run_tool = query_agent._run_tool

    def fake_load_index(space_id: str) -> list[dict[str, Any]]:
        """函数功能：`fake_load_index` 负责加载 index，服务于本文件职责：Agent 问答评测。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
        """
        return notes

    def fake_semantic_search(space_id: str, query: str, top_k: int = 5, min_score: float = query_agent.DEFAULT_QUERY_MIN_SCORE):
        """函数功能：`fake_semantic_search` 负责搜索 fake semantic，服务于本文件职责：Agent 问答评测。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            query: 检索或查询文本，类型为 `str`。
            top_k: top k 参数，由调用方传入，类型为 `int`，默认值为 `5`。
            min_score: min score 参数，由调用方传入，类型为 `float`，默认值为 `query_agent.DEFAULT_QUERY_MIN_SCORE`。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        return local_semantic_search(notes, query, top_k, min_score)

    def recording_run_tool(space_id: str, action: str, args: dict[str, Any]) -> Any:
        """函数功能：`recording_run_tool` 负责运行 tool，服务于本文件职责：Agent 问答评测。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            action: action 参数，由调用方传入，类型为 `str`。
            args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        result = original_run_tool(space_id, action, args)
        tool_calls.append({"tool": action, "args": args, "result": result})
        return result

    try:
        query_agent.load_index = fake_load_index
        query_agent.semantic_search = fake_semantic_search
        query_agent._run_tool = recording_run_tool
        answer = query_agent.answer_question(
            "eval_space",
            str(case.get("question", "")),
            max_steps=int(case.get("max_steps", 4)),
        )
    finally:
        query_agent.load_index = original_load_index
        query_agent.semantic_search = original_semantic_search
        query_agent._run_tool = original_run_tool

    score = score_query_react(tool_calls, answer, case)
    score["question"] = case.get("question")
    return score


def run(cases_path: Path, *, dry_run: bool = False, max_cases: int | None = None) -> dict[str, object]:
    """函数功能：`run` 负责运行，服务于本文件职责：Agent 问答评测。
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

    results = [run_case(case) for case in cases]
    return {
        "mode": "query_react",
        "summary": aggregate_boolean_scores(results),
        "results": results,
    }


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：Agent 问答评测。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    parser = argparse.ArgumentParser(description="Evaluate complete ReAct query behavior.")
    parser.add_argument("--cases", default="eval/data/query_cases.jsonl")
    parser.add_argument("--output", default="eval/results/query_react_results.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate cases without calling LLM/embedding API")
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()

    report = run(Path(args.cases), dry_run=args.dry_run, max_cases=args.max_cases)
    write_json(args.output, report)
    print(f"Wrote {args.output}")
    print(report.get("summary", report))


if __name__ == "__main__":
    main()
