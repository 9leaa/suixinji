#!/usr/bin/env python3
"""Read-only audit for likely unsafe historical memory merges.

The script only reports identifiers and reason codes.  It never writes to the
database, never invokes an LLM, and deliberately avoids printing memory text.
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

from memory.models import MEMORY_KEY_V3_VERSION, normalize_content
from memory.repository import list_memories


def audit_space(space_id: str, *, limit: int = 2_000) -> dict[str, Any]:
    """负责“审计空间”。

    该函数是 `scripts.audit_memory_false_merges` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    findings: list[dict[str, Any]] = []
    scanned = list_memories(space_id, status=None, limit=max(1, min(int(limit), 10_000)))
    keys: dict[tuple[str, str], list[Any]] = {}
    for memory in scanned:
        keys.setdefault((memory.memory_type, memory.effective_memory_key), []).append(memory)
        predicate = normalize_content(memory.predicate or "")
        if memory.memory_type == "semantic" and predicate in {"", "fact", "事实"} and len(memory.sources) > 1:
            findings.append({"memory_id": memory.id, "reason": "generic_semantic_fact_multiple_sources"})
        if memory.memory_type == "task" and memory.memory_key_version != MEMORY_KEY_V3_VERSION and len(memory.sources) > 1:
            findings.append({"memory_id": memory.id, "reason": "legacy_task_key_multiple_sources"})

    for (memory_type, key), memories in keys.items():
        if len(memories) < 2:
            continue
        # Multiple active rows on an identical V3 key can be a retry artifact
        # or a previously unsafe merge/split.  Report it for human review.
        active = [memory for memory in memories if memory.status == "active"]
        if len(active) > 1 and any(memory.memory_key_version == MEMORY_KEY_V3_VERSION for memory in active):
            for memory in active:
                findings.append({"memory_id": memory.id, "reason": "duplicate_active_canonical_key", "memory_type": memory_type})

    deduped = list({(item["memory_id"], item["reason"]): item for item in findings}.values())
    reason_counts = Counter(item["reason"] for item in deduped)
    return {
        "mode": "read_only",
        "space_id": space_id,
        "scanned_count": len(scanned),
        "finding_count": len(deduped),
        "reason_counts": dict(sorted(reason_counts.items())),
        "findings": sorted(deduped, key=lambda item: (item["reason"], item["memory_id"]))[:1_000],
    }


def main() -> int:
    """作为脚本入口，解析运行参数并启动本模块定义的处理流程。"""
    parser = argparse.ArgumentParser(description="Read-only Memory V3 false-merge audit")
    parser.add_argument("--space-id", required=True, help="target space; required to avoid accidental global scans")
    parser.add_argument("--limit", type=int, default=2_000)
    parser.add_argument("--output", help="optional JSON file path")
    args = parser.parse_args()
    report = audit_space(args.space_id, limit=args.limit)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
