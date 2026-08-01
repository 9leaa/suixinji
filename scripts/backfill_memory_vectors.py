#!/usr/bin/env python3
"""文件作用：Memory 向量回填。

项目关系：本文件依赖 `repositories.postgres.memory`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from repositories.postgres.memory import schedule_memory_vector_backfill


def main() -> int:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：Memory 向量回填。
    传参：
        无。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", default="active", choices=("active", "inactive", "archived"))
    parser.add_argument("--limit", type=int, default=10000)
    args = parser.parse_args()
    scheduled = schedule_memory_vector_backfill(status=args.status, limit=args.limit)
    print(json.dumps({"status": "ok", "scheduled": scheduled, "memory_status": args.status}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
