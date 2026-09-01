"""Judge paired LongMemEval outputs with the project's configured fast LLM.

This is a reproducible non-official semantic judge for comparing two Suixinji
paths under exactly the same prompt.  It is not the LongMemEval GPT-4o judge.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.llm_client import complete_json


SYSTEM_PROMPT = """You are a strict evaluator for a long-term conversational-memory QA benchmark.
Return JSON only: {"correct": true|false, "reason": "short"}.

Evaluate the candidate answer against the question and Gold answer.
- Mark true only if it gives the Gold answer or a semantically equivalent complete answer.
- A response that merely repeats the question, says information is missing, or gives only a subset is false.
- For knowledge updates, an answer is correct only if it gives the required updated/current value.
- For temporal and count questions, it must give the requested comparison or total correctly.
- For preference questions, it is correct when it uses the relevant user preference accurately in its recommendation.
- Do not treat provenance IDs, source lists, or unrelated numbers as an answer.
"""


def _answer_body(value: object) -> str:
    return str(value or "").split("\n\n来源", 1)[0].strip()


def _judge(row: dict[str, Any], field: str) -> dict[str, Any]:
    answer = _answer_body(row.get(field))
    payload = {
        "question_type": row.get("question_type"),
        "question": row.get("question"),
        "gold_answer": row.get("gold_answer"),
        "candidate_answer": answer,
    }
    try:
        data = complete_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=json.dumps(payload, ensure_ascii=False),
            model_role="fast",
            llm_task="query_synthesis",
        )
        return {
            "correct": bool(data.get("correct")),
            "reason": str(data.get("reason") or "")[:300],
            "error": None,
        }
    except Exception as exc:
        return {"correct": None, "reason": "", "error": f"{type(exc).__name__}: {exc}"}


def _rate(rows: list[dict[str, Any]], key: str) -> dict[str, int | float]:
    labels = [row[key]["correct"] for row in rows if row.get(key, {}).get("correct") is not None]
    return {
        "judged": len(labels),
        "correct": sum(bool(label) for label in labels),
        "accuracy": round(sum(bool(label) for label in labels) / len(labels), 4) if labels else 0.0,
        "errors": sum(bool(row.get(key, {}).get("error")) for row in rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.input.open(encoding="utf-8")]
    jobs = [(index, field) for index in range(len(rows)) for field in ("answer", "react_answer")]
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        futures = {pool.submit(_judge, rows[index], field): (index, field) for index, field in jobs}
        for future in as_completed(futures):
            index, field = futures[future]
            target = "ask_v2_judge" if field == "answer" else "react_judge"
            rows[index][target] = future.result()
            print(json.dumps({"question_id": rows[index]["question_id"], "path": target, "done": True}), flush=True)

    output = args.input.with_name(args.input.stem + "_llm_judged.jsonl")
    with output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "benchmark": "LongMemEval-Oracle cleaned paired A/B",
        "judge": "Suixinji configured fast LLM; non-official semantic judge",
        "ask_v2": _rate(rows, "ask_v2_judge"),
        "react": _rate(rows, "react_judge"),
        "output": str(output),
    }
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
