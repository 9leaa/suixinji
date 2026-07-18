#!/usr/bin/env python
"""Delete one Stage 4 test tenant and its dedicated Redis namespace."""

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--redis-env", required=True)
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args()


def main() -> None:
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
