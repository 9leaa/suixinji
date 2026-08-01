#!/usr/bin/env python
"""文件作用：Stage 4 测试清理。

项目关系：本文件依赖 `infrastructure.database`、`infrastructure.redis_client`、`infrastructure.redis_keys`、`infrastructure.schema`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infrastructure.database import session_scope
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import RedisKeys
from infrastructure.schema import OutboxEvent, Task, Tenant


def parse_args() -> argparse.Namespace:
    """函数功能：`parse_args` 负责解析 args，服务于本文件职责：Stage 4 测试清理。
    传参：
        无。
    返回结果说明：
        返回 `argparse.Namespace` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--redis-env", required=True)
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args()


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：Stage 4 测试清理。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    args = parse_args()
    if not args.confirm:
        raise SystemExit("--confirm is required")
    if not args.tenant_id.startswith("load-") or not args.redis_env.startswith("stage4-"):
        raise SystemExit("refusing to clean a non-Stage-4 tenant or Redis namespace")
    with session_scope() as session:
        task_ids = list(session.execute(select(Task.id).where(Task.tenant_id == args.tenant_id)).scalars())
        outbox_deleted = 0
        if task_ids:
            result = session.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id.in_(task_ids)))
            outbox_deleted = int(result.rowcount or 0)
        tenant_deleted = int(session.execute(delete(Tenant).where(Tenant.id == args.tenant_id)).rowcount or 0)

    client = get_redis()
    prefix = RedisKeys(env=args.redis_env).prefix
    keys = list(client.scan_iter(match=f"{prefix}:*"))
    redis_deleted = int(client.delete(*keys)) if keys else 0
    report = {
        "tenant_id": args.tenant_id,
        "task_ids": len(task_ids),
        "outbox_deleted": outbox_deleted,
        "tenant_deleted": tenant_deleted,
        "redis_namespace": prefix,
        "redis_keys_deleted": redis_deleted,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
