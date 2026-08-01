#!/usr/bin/env python
"""文件作用：多用户压测。

项目关系：本文件依赖 `runtime.load_testing`；被 `tests.test_api_bind_config`。
"""



from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.load_testing import PROFILES, execute_load, generate_requests, summarize_plan


def default_endpoint() -> str:
    """函数功能：`default_endpoint` 负责处理 default endpoint，服务于本文件职责：多用户压测。
    传参：
        无。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    values = dotenv_values(ROOT / ".env")
    host = os.environ.get("SUIXINJI_API_HOST") or values.get("SUIXINJI_API_HOST") or "127.0.0.1"
    port = os.environ.get("SUIXINJI_API_PORT") or values.get("SUIXINJI_API_PORT") or "8000"
    return f"http://{str(host).strip() or '127.0.0.1'}:{str(port).strip() or '8000'}"


def parse_args() -> argparse.Namespace:
    """函数功能：`parse_args` 负责解析 args，服务于本文件职责：多用户压测。
    传参：
        无。
    返回结果说明：
        返回 `argparse.Namespace` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(PROFILES), default="smoke")
    parser.add_argument("--users", type=int)
    parser.add_argument("--messages-per-user", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--endpoint", action="append", dest="endpoints")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=20260718)
    parser.add_argument("--run-id")
    parser.add_argument("--output")
    parser.add_argument("--execute", action="store_true", help="Actually submit requests. Without this flag only the workload plan is printed.")
    return parser.parse_args()


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：多用户压测。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    args = parse_args()
    profile = PROFILES[args.profile]
    users = args.users or profile.users
    messages_per_user = args.messages_per_user or profile.messages_per_user
    concurrency = args.concurrency or profile.concurrency
    requests = generate_requests(
        users=users,
        messages_per_user=messages_per_user,
        run_id=args.run_id,
        seed=args.seed,
    )
    if not args.execute:
        report = {
            **summarize_plan(requests),
            "mode": "dry_run",
            "concurrency": concurrency,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    else:
        endpoints = args.endpoints or [default_endpoint()]
        report = execute_load(
            requests,
            endpoint=endpoints,
            concurrency=concurrency,
            timeout_seconds=args.timeout_seconds,
        )
        report["mode"] = "executed"
        report["endpoints"] = endpoints
    output = Path(args.output) if args.output else ROOT / "data" / "load-tests" / f"{requests[0].run_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={output}")


if __name__ == "__main__":
    main()
