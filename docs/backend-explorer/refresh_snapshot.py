"""Read-only local status snapshot; exports no credentials or business content."""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from sqlalchemy import inspect, text
from infrastructure.database import get_engine
from infrastructure.schema import Base
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import KEYS
from apps.handlers import HANDLERS
from core import settings as s

e = get_engine()
inspector = inspect(e)
d = {
    "sampled_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    "services": [],
    "tables": [],
    "streams": [],
    "settings": {},
}
for role in ["receiver", "api", "outbox-relay", "scheduler", *["worker-" + x.replace("_", "-") for x in HANDLERS]]:
    p = ROOT / "data/pids" / f"{role}.pid"
    pid = int(p.read_text()) if p.exists() else None
    try:
        os.kill(pid, 0)
        cmd = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
        running = str(ROOT) in cmd
    except (OSError, TypeError, subprocess.SubprocessError):
        running = False
    d["services"].append({"role": role, "pid": pid, "running": running, "pool": s.database_pool_budget(role)})
with e.connect() as c:
    d["revision"] = c.execute(text("select version_num from alembic_version")).scalar()
    d["extensions"] = [list(r) for r in c.execute(text("select extname,extversion from pg_extension"))]
    for name, t in sorted(Base.metadata.tables.items()):
        d["tables"].append(
            {
                "name": name,
                "count": c.execute(text('select count(*) from "' + name + '"')).scalar(),
                "columns": [{"name": x.name, "type": str(x.type), "pk": x.primary_key, "nullable": x.nullable} for x in t.columns],
                "fks": [{"columns": list(f.column_keys), "target": f.referred_table.name} for f in t.foreign_key_constraints],
                "indexes": [x["name"] for x in inspector.get_indexes(name)],
            }
        )
    d["db_connections"] = c.execute(text("select count(*) from pg_stat_activity where datname=current_database()")).scalar()
r = get_redis()
for typ in HANDLERS:
    key = KEYS.stream(typ)
    groups = r.xinfo_groups(key) if r.exists(key) else []
    d["streams"].append(
        {
            "type": typ,
            "key": key,
            "length": r.xlen(key),
            "groups": [{"name": g["name"], "pending": g["pending"], "consumers": g["consumers"], "lag": g.get("lag")} for g in groups],
        }
    )
d["redis"] = {
    "version": r.info("server")["redis_version"],
    "aof": bool(r.info("persistence")["aof_enabled"]),
    "keys": r.dbsize(),
    "prefix": KEYS.prefix,
}
for k in [
    "STORAGE_BACKEND",
    "COORDINATION_BACKEND",
    "TASK_QUEUE_BACKEND",
    "API_HOST",
    "API_PORT",
    "STREAM_BLOCK_MS",
    "STREAM_BATCH_SIZE",
    "STREAM_CLAIM_IDLE_MS",
    "STREAM_MAXLEN",
    "OUTBOX_BATCH_SIZE",
    "OUTBOX_POLL_INTERVAL_SECONDS",
    "TASK_LEASE_SECONDS",
    "OUTBOX_LEASE_SECONDS",
    "WORKER_MAX_ATTEMPTS",
    "WORKER_RETRY_BASE_SECONDS",
    "OUTBOX_MAX_ATTEMPTS",
    "SCHEDULER_LEADER_TTL_MS",
    "DATABASE_GLOBAL_BUDGET",
    "IDEMPOTENCY_TTL_SECONDS",
    "SESSION_TTL_SECONDS",
    "CACHE_SEARCH_TTL_SECONDS",
    "EMBEDDING_CACHE_TTL_SECONDS",
    "LLM_CONCURRENCY_LIMIT",
    "RATE_LIMIT_ASK_PER_MINUTE",
    "RATE_LIMIT_INGEST_PER_MINUTE",
    "FAKE_EXTERNALS",
    "TEST_API_ENABLED",
]:
    d["settings"][k] = getattr(s, k)
try:
    d["api_health"] = json.load(urllib.request.urlopen(f"http://{s.API_HOST}:{s.API_PORT}/health", timeout=3))
except OSError:
    d["api_health"] = {"status": "unavailable"}
folder = Path(__file__).parent
(folder / "snapshot.json").write_text(json.dumps(d, ensure_ascii=False, indent=2))
import re

payload = json.dumps(d, ensure_ascii=False).replace("<", "\u003c")
for filename in ["index.html", "canvas.html"]:
    page = folder / filename
    if page.exists():
        source = page.read_text()
        page.write_text(
            re.sub(r'(<script id="snapshot" type="application/json">).*?(</script>)', lambda m: m[1] + payload + m[2], source, flags=re.S)
        )
print("Snapshot:", d["sampled_at"], "tables:", len(d["tables"]), "running:", sum(x["running"] for x in d["services"]))
print("Tables:", ", ".join(t["name"] for t in d["tables"]))
