#!/usr/bin/env python
"""文件作用：分布式指标采集 CLI。

项目关系：本文件依赖 `runtime.distributed_metrics`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.distributed_metrics import (
    build_report,
    collect_database_metrics,
    collect_lock_metrics,
    collect_stream_metrics,
    reconcile_retry_submission,
)


def parse_args() -> argparse.Namespace:
    """函数功能：`parse_args` 负责解析 args，服务于本文件职责：分布式指标采集 CLI。
    传参：
        无。
    返回结果说明：
        返回 `argparse.Namespace` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--submission-report")
    parser.add_argument("--retry-submission-report", action="append", default=[])
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：分布式指标采集 CLI。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    args = parse_args()
    submission = {}
    if args.submission_report:
        submission = json.loads(Path(args.submission_report).read_text(encoding="utf-8"))
    database = collect_database_metrics(args.tenant_id)
    retries = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.retry_submission_report]
    submission = reconcile_retry_submission(
        submission,
        retries,
        database_accepted=int(database.get("accepted") or 0),
    )
    streams = collect_stream_metrics()
    locks = collect_lock_metrics(since=submission.get("started_at"))
    report = build_report(database, streams, submission=submission, locks=locks)
    report["tenant_id"] = args.tenant_id
    output = Path(args.output) if args.output else ROOT / "data" / "load-tests" / f"{args.tenant_id}-metrics.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={output}")


if __name__ == "__main__":
    main()
