"""Prewarm the normal embedding cache for a retrieval evaluation dataset."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.llm_client import embed_text
from eval.layer3.run_layer3_eval import _candidate_for, load_cases_with_manifest
from memory.vector_lifecycle import memory_embedding_text


def _memory_text(raw: dict[str, Any]) -> str:
    candidate = _candidate_for(raw, "prewarm")
    return memory_embedding_text(
        memory_type=candidate.memory_type,
        subject=candidate.subject,
        predicate=candidate.predicate,
        object_value=candidate.object_value,
        content=candidate.content,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    cases, manifest = load_cases_with_manifest(args.data_dir)
    if args.limit > 0:
        cases = cases[:args.limit]
    texts: dict[str, str] = {}
    for case in cases:
        query = str((case.get("input") or {}).get("query") or "").strip()
        if query:
            texts.setdefault(query, "query")
        snapshot = (case.get("input") or {}).get("memory_snapshot") or {}
        for raw in snapshot.get("memories") or []:
            value = _memory_text(raw).strip()
            if value:
                texts.setdefault(value, "memory")
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []

    def run(item: tuple[str, str]) -> dict[str, Any]:
        text, kind = item
        begin = time.perf_counter()
        try:
            embedding = embed_text(text)
            return {
                "kind": kind, "status": "ok", "dimension": len(embedding),
                "latency_ms": round((time.perf_counter() - begin) * 1000, 3),
            }
        except Exception as exc:
            return {
                "kind": kind, "status": "error", "error_type": type(exc).__name__,
                "latency_ms": round((time.perf_counter() - begin) * 1000, 3),
            }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(run, item) for item in texts.items()]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            rows.append(future.result())
            if index % 20 == 0 or index == len(futures):
                print(f"prewarm={index}/{len(futures)}", flush=True)
    latencies = [float(row["latency_ms"]) for row in rows]
    result = {
        "dataset": args.data_dir,
        "contract": manifest,
        "case_count": len(cases),
        "unique_text_count": len(texts),
        "workers": args.workers,
        "success_count": sum(row["status"] == "ok" for row in rows),
        "error_count": sum(row["status"] != "ok" for row in rows),
        "kind_counts": {
            kind: sum(row["kind"] == kind for row in rows)
            for kind in sorted({str(row["kind"]) for row in rows})
        },
        "mean_latency_ms": round(statistics.fmean(latencies), 3) if latencies else 0.0,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "errors": [row for row in rows if row["status"] != "ok"],
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
