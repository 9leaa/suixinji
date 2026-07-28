"""Run a live, isolated retrieval + LLM answer evaluation.

This evaluator writes only to a fresh PostgreSQL ``space_id`` prefixed with
``eval_live_``. It uses the real embedding endpoint for Note/Memory vectors
and the real configured LLM for the answer cases. The original Feishu space
is never read or modified.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import query_agent
from core.config import get_embedding_config
from core.llm_client import embed_text
from eval.common import write_json
from infrastructure.database import session_scope
from infrastructure.schema import MemoryVector, Space
from memory.models import MemoryCandidate
from repositories.postgres.memory import (
    complete_memory_vector,
    insert_memory,
)
from repositories.postgres.notes import save_note
from repositories.postgres.vectors import add_vector_item
from sqlalchemy import delete
from storage.vector_store import VectorItem


DATASET_PATH = ROOT / "eval" / "data" / "live_retrieval_cases.json"


def _now(offset: int = 0) -> str:
    """负责“now”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return (datetime.now().astimezone() - timedelta(seconds=offset)).isoformat()


def _note_text(note: dict[str, Any]) -> str:
    """负责“笔记文本”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    return "\n".join(
        str(value)
        for value in (
            note.get("title"),
            note.get("type"),
            " ".join(note.get("tags") or []),
            note.get("summary"),
            note.get("text"),
        )
        if value
    )


def _insert_notes(space_id: str, notes: list[dict[str, Any]]) -> int:
    """负责“插入笔记列表”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    model = str(get_embedding_config().model)
    created = 0
    for index, note in enumerate(notes):
        note_id = f"{space_id}_{note['id']}"
        meta = {
            "id": note_id,
            "message_id": f"{space_id}_msg_{note['id']}",
            "space_id": space_id,
            "tenant_id": "default",
            "ts": _now(index),
            "title": note["title"],
            "tags": note.get("tags", []),
            "type": note.get("type", "other"),
            "summary": note.get("summary", ""),
            "text": note["text"],
            "enrichment_status": "ready",
            "sensitivity": "normal",
        }
        created += int(save_note(meta))
        embedding = embed_text(_note_text(note))
        add_vector_item(
            space_id,
            VectorItem(
                note_id=note_id,
                message_id=meta["message_id"],
                text=_note_text(note),
                embedding=embedding,
                metadata={
                    "title": note["title"],
                    "type": note.get("type", "other"),
                    "tags": note.get("tags", []),
                    "summary": note.get("summary", ""),
                    "ts": meta["ts"],
                    "message_id": meta["message_id"],
                    "embedding_model": model,
                },
            ),
        )
    return created


def _ensure_memory_vector(memory_id: str, *, timeout: float = 90.0) -> None:
    """Let the normal worker claim the vector, or complete it in this process."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with session_scope() as session:
            row = session.get(MemoryVector, memory_id)
            if row is not None and row.status == "ready" and row.embedding is not None:
                return
        # The distributed embedding worker normally handles this task. If it
        # has not claimed it yet, claim and complete through the same API so
        # the evaluation is not coupled to worker scheduling.
        from repositories.postgres.memory import claim_memory_vector

        claim = claim_memory_vector(memory_id)
        if claim is not None:
            embedding = embed_text(str(claim["text"]))
            complete_memory_vector(
                memory_id,
                content_hash=str(claim["content_hash"]),
                embedding=embedding,
                model=str(claim["model"]),
                dimension=int(claim["dimension"]),
                embedding_version=str(claim["embedding_version"]),
            )
            return
        time.sleep(0.4)
    raise TimeoutError(f"memory vector not ready: {memory_id}")


def _insert_memories(space_id: str, memories: list[dict[str, Any]]) -> dict[str, str]:
    """负责“插入memories”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    ids: dict[str, str] = {}
    for item in memories:
        candidate = MemoryCandidate(
            memory_type=str(item["memory_type"]),
            content=str(item["content"]),
            importance=0.9,
            confidence=0.95,
            task_status=item.get("task_status"),
            subject=item.get("subject"),
            predicate=item.get("predicate"),
            object_value=item.get("object_value"),
            polarity=item.get("polarity"),
            note_id=f"{space_id}_source_{item['id']}",
            space_id=space_id,
            extractor_type="live_eval",
        )
        record = insert_memory(
            space_id,
            candidate,
            source_note_id=f"{space_id}_source_{item['id']}",
        )
        ids[str(item["id"])] = record.id
    for memory_id in ids.values():
        _ensure_memory_vector(memory_id)
    return ids


def _metrics(ranks: list[int | None], *, cutoff: int = 5) -> dict[str, float]:
    """负责“指标”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    valid = [rank for rank in ranks if rank is not None]
    return {
        "cases": float(len(ranks)),
        "hit_rate": round(sum(rank is not None and rank <= cutoff for rank in ranks) / len(ranks), 4) if ranks else 0.0,
        "recall_at_1": round(sum(rank == 1 for rank in ranks) / len(ranks), 4) if ranks else 0.0,
        "recall_at_3": round(sum(rank is not None and rank <= 3 for rank in ranks) / len(ranks), 4) if ranks else 0.0,
        "recall_at_5": round(sum(rank is not None and rank <= 5 for rank in ranks) / len(ranks), 4) if ranks else 0.0,
        "mrr": round(sum(1.0 / rank for rank in valid) / len(ranks), 4) if ranks else 0.0,
    }


def _latency_metrics(results: list[dict[str, Any]]) -> dict[str, float]:
    """负责“latency指标”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    values = sorted(float(item["latency_ms"]) for item in results)
    if not values:
        return {"avg_latency_ms": 0.0, "p95_latency_ms": 0.0}
    p95_index = min(len(values) - 1, max(0, math.ceil(len(values) * 0.95) - 1))
    return {
        "avg_latency_ms": round(sum(values) / len(values), 2),
        "p95_latency_ms": round(values[p95_index], 2),
    }


def _run_note_eval(space_id: str, cases: list[dict[str, Any]], id_map: dict[str, str]) -> dict[str, Any]:
    """负责“运行笔记评测”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    results: list[dict[str, Any]] = []
    ranks: list[int | None] = []
    for case in cases:
        started = time.perf_counter()
        rows = query_agent.semantic_search(space_id, str(case["query"]), top_k=5)
        ranked = [str(row.get("id")) for row in rows]
        expected = [id_map[item] for item in case["expected_ids"]]
        rank = next((index + 1 for index, note_id in enumerate(ranked) if note_id in expected), None)
        ranks.append(rank)
        results.append(
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "expected_ids": expected,
                "ranked_ids": ranked,
                "rank": rank,
                "channels": [row.get("retrieval_channels", []) for row in rows],
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
    return {"metrics": _metrics(ranks) | _latency_metrics(results), "results": results}


def _run_memory_eval(space_id: str, cases: list[dict[str, Any]], id_map: dict[str, str]) -> dict[str, Any]:
    """负责“运行记忆评测”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    results: list[dict[str, Any]] = []
    ranks: list[int | None] = []
    status_correct = 0
    polarity_correct = 0
    status_total = 0
    polarity_total = 0
    from repositories.postgres.memory import search_memories

    for case in cases:
        started = time.perf_counter()
        rows = search_memories(space_id, str(case["query"]), limit=5, mark_access=False)
        records = [record for record, _score in rows]
        ranked = [record.id for record in records]
        expected = [id_map[item] for item in case["expected_ids"]]
        rank = next((index + 1 for index, memory_id in enumerate(ranked) if memory_id in expected), None)
        ranks.append(rank)
        expected_status = case.get("expected_status")
        if expected_status:
            status_total += 1
            status_correct += int(bool(records) and records[0].task_status == expected_status)
        expected_polarity = case.get("expected_polarity")
        if expected_polarity:
            polarity_total += 1
            polarity_correct += int(bool(records) and records[0].polarity == expected_polarity)
        results.append(
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "expected_ids": expected,
                "ranked_ids": ranked,
                "rank": rank,
                "top_content": records[0].content if records else None,
                "top_status": records[0].task_status if records else None,
                "top_polarity": records[0].polarity if records else None,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
    metrics = _metrics(ranks) | _latency_metrics(results)
    metrics.update(
        {
            "state_accuracy": round(status_correct / status_total, 4) if status_total else 0.0,
            "state_cases": float(status_total),
            "polarity_accuracy": round(polarity_correct / polarity_total, 4) if polarity_total else 0.0,
            "polarity_cases": float(polarity_total),
        }
    )
    return {"metrics": metrics, "results": results}


def _run_llm_eval(space_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """负责“运行LLM评测”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    original = query_agent._complete_json_with_hooks
    call_count = 0

    def counted(*args: Any, **kwargs: Any) -> Any:
        """负责“counted”。

        该函数是 `eval.live_retrieval_eval` 中的`_run_llm_eval` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        nonlocal call_count
        call_count += 1
        return original(*args, **kwargs)

    query_agent._complete_json_with_hooks = counted
    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            started = time.perf_counter()
            before = call_count
            error = None
            try:
                answer = query_agent.answer_question(space_id, str(case["question"]), max_steps=4)
            except Exception as exc:  # record the failure instead of aborting the set
                answer = ""
                error = f"{type(exc).__name__}: {exc}"
            groups = case.get("must_include", [])
            group_results = [any(term in answer for term in group) for group in groups]
            results.append(
                {
                    "case_id": case["case_id"],
                    "question": case["question"],
                    "answer": answer,
                    "passed": bool(not error and all(group_results)),
                    "must_include_results": group_results,
                    "llm_calls": call_count - before,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error": error,
                }
            )
    finally:
        query_agent._complete_json_with_hooks = original
    latencies = [item["latency_ms"] for item in results]
    return {
        "metrics": {
            "cases": len(results),
            "answer_accuracy": round(sum(bool(item["passed"]) for item in results) / len(results), 4) if results else 0.0,
            "llm_calls": call_count,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            "p95_latency_ms": round(sorted(latencies)[min(len(latencies) - 1, max(0, math.ceil(len(latencies) * 0.95) - 1))], 2) if latencies else 0.0,
        },
        "results": results,
    }


def _cleanup_space(space_id: str) -> None:
    """负责“cleanup空间”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    with session_scope() as session:
        session.execute(delete(Space).where(Space.id == space_id))


def run(*, keep: bool = True) -> dict[str, Any]:
    """负责“运行”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    space_id = f"eval_live_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    note_ids = {item["id"]: f"{space_id}_{item['id']}" for item in dataset["notes"]}
    memory_ids: dict[str, str] = {}
    created_notes = _insert_notes(space_id, dataset["notes"])
    memory_ids = _insert_memories(space_id, dataset["memories"])
    report = {
        "dataset_version": dataset["version"],
        "space_id": space_id,
        "kept": keep,
        "created_notes": created_notes,
        "created_memories": len(memory_ids),
        "real_embedding": True,
        "real_llm": True,
        "note": _run_note_eval(space_id, dataset["note_cases"], note_ids),
        "memory": _run_memory_eval(space_id, dataset["memory_cases"], memory_ids),
        "llm": _run_llm_eval(space_id, dataset["llm_cases"]),
    }
    if not keep:
        _cleanup_space(space_id)
        report["db_space_cleaned"] = True
    return report


def _print_report(report: dict[str, Any]) -> None:
    """负责“print报告”。

    该函数是 `eval.live_retrieval_eval` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    print(json.dumps({key: report[key] for key in ("dataset_version", "space_id", "created_notes", "created_memories", "real_embedding", "real_llm")}, ensure_ascii=False))
    print("section\tcases\thit_rate\trecall@1\trecall@3\trecall@5\tMRR\tstate_acc\tpolarity_acc\tanswer_acc\tavg_ms\tp95_ms")
    for name in ("note", "memory"):
        metrics = report[name]["metrics"]
        print(f"{name}\t{int(metrics['cases'])}\t{metrics['hit_rate']:.4f}\t{metrics['recall_at_1']:.4f}\t{metrics['recall_at_3']:.4f}\t{metrics['recall_at_5']:.4f}\t{metrics['mrr']:.4f}\t{metrics.get('state_accuracy', 0):.4f}\t{metrics.get('polarity_accuracy', 0):.4f}\t-\t{metrics['avg_latency_ms']:.2f}\t{metrics['p95_latency_ms']:.2f}")
    metrics = report["llm"]["metrics"]
    print(f"llm\t{int(metrics['cases'])}\t-\t-\t-\t-\t-\t-\t-\t{metrics['answer_accuracy']:.4f}\t{metrics['avg_latency_ms']:.2f}\t{metrics['p95_latency_ms']:.2f}")


def main() -> None:
    """作为脚本入口，解析运行参数并启动本模块定义的处理流程。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    parser.add_argument("--keep", action="store_true", help="keep the isolated evaluation space (default behavior is also isolated)")
    args = parser.parse_args()
    report = run(keep=bool(args.keep))
    output = Path(args.output) if args.output else ROOT / "eval" / "results" / f"live_retrieval_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    write_json(output, report)
    dataset_stem = output.stem.replace("_eval_", "_dataset_", 1)
    write_json(output.with_name(dataset_stem + ".json"), json.loads(DATASET_PATH.read_text(encoding="utf-8")) | {"space_id": report["space_id"]})
    _print_report(report)
    print(f"report={output}")


if __name__ == "__main__":
    main()
