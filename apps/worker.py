"""文件作用：分布式 worker CLI 入口。

项目关系：本文件依赖 `apps.handlers`、`core.observability`、`runtime.delivery_store`、`runtime.streams.worker`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import logging

from apps.handlers import HANDLERS
from core.observability import log_process_started
from runtime.delivery_store import recover_stale_reserved_deliveries
from runtime.streams.worker import AdaptiveStreamWorker, StreamWorker


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：分布式 worker CLI 入口。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("task_type", choices=[*sorted(HANDLERS), "adaptive"])
    parser.add_argument("--worker-id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    log_process_started(f"worker-{args.task_type}", action="runtime.worker_started")
    if args.task_type in {"delivery", "adaptive"}:
        recover_stale_reserved_deliveries()
    if args.task_type == "adaptive":
        AdaptiveStreamWorker(HANDLERS, worker_id=args.worker_id).run_forever()
    else:
        StreamWorker(args.task_type, HANDLERS[args.task_type], worker_id=args.worker_id).run_forever()


if __name__ == "__main__":
    main()
