#!/usr/bin/env python3
"""文件作用：记忆错误合并审计。

项目关系：本文件依赖 `memory.models`、`memory.repository`；被 暂无静态导入方或仅作为入口脚本执行。
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
    """函数功能：`audit_space` 负责处理 audit space，服务于本文件职责：记忆错误合并审计。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `2000`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
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
        # 同一个 V3 key 下出现多条 active 记录，可能是重试产物或早期不安全合并/拆分结果，需要报告给人工复核。
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
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：记忆错误合并审计。
    传参：
        无。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
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
