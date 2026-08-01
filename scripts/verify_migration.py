"""文件作用：迁移校验。

项目关系：本文件依赖 `scripts.migrate_local_to_postgres`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.migrate_local_to_postgres import _collect, _database_counts, _local_counts


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：迁移校验。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    payload, failures = _collect(args.data_dir)
    local = _local_counts(payload)
    postgres = _database_counts()
    checks = {
        name: {"local": count, "postgres": postgres.get(name, 0), "ok": postgres.get(name, 0) >= count}
        for name, count in local.items()
        if name in postgres
    }
    report = {"status": "ok" if all(item["ok"] for item in checks.values()) and not failures else "mismatch", "checks": checks, "read_failures": failures}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
